"""The transmute loop — one tick of `alchemist run-once`.

End-to-end:
    scan → group by repo → fan out (per-repo lock) → for each issue:
        clone → render brief → conductor exec → push → open PR →
        delegate to touchstone's `merge-pr.sh` (review-and-merge gate)

Composition is by subprocess, never by code import (Doctrine 0001/0003/0004).
Alchemist owns NONE of:
- The agent's decision-making about how to fix the issue (Conductor + the brief).
- The review-and-merge gate (Touchstone's `merge-pr.sh`).

Alchemist owns ONLY:
- GitHub I/O (issue scan, label transitions, PR open).
- Git plumbing (clone, branch, commit, push).
- Per-repo lock + cross-repo fan-out.
- Hand-offs between the above.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from alchemist.briefs import BRIEF_TEMPLATE_VERSION, render_brief, render_pr_body
from alchemist.doctor import run_doctor
from alchemist.locks import LockBusyError, acquire, is_locked
from alchemist.reporter import report_tool_failure
from alchemist.scanner import DispatchIssue, scan

if TYPE_CHECKING:
    from alchemist.config import Config


# Audit signing — every git commit + PR title surfaces alchemist clearly.
# v0.1 uses these directly (PAT-based GITHUB_TOKEN authoring).
# v0.2 (alchemist#6) swaps to GitHub App installation tokens; commits authored
# via the App will additionally show as the App's bot user in the GitHub UI.
_GIT_AUTHOR_NAME = "Alchemist"
_GIT_AUTHOR_EMAIL = "alchemist@autumngarage.dev"
_PR_TITLE_PREFIX = "[alchemist]"

# Stuck-state sweep/lock lease threshold is derived from the configured worker
# and merge-review timeouts plus a small buffer. Never use less than the legacy
# 30-minute floor while dogfooding.
_MIN_STUCK_SWEEP_THRESHOLD_SEC = 30 * 60
_STUCK_SWEEP_MARGIN_SEC = 10 * 60
_TARGET_DEP_PREP_TIMEOUT_SEC = 10 * 60
_SKIP_LABEL = "alchemist-skip"

# Per-process cache: only ensure a repo+dispatch label set once per process.
_LABELS_ENSURED: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class RunResult:
    repo: str
    issue_number: int
    pr_url: str | None
    merged: bool | None      # True/False after merge-pr.sh ran; None on dry-run/no-PR
    error: str | None
    elapsed_sec: float
    dry_run: bool
    branch: str | None = None


@dataclass(frozen=True)
class _MergeGateResult:
    merged: bool
    error: str | None = None


def run_tick(config: Config) -> list[RunResult]:
    """Process one tick worth of dispatched issues.

    Issues are grouped by repo. Within a repo, only one worker runs at a
    time (the per-repo lock is the constraint). Across repos, up to
    `max_concurrent_repos` workers run in parallel — that's the swarm.

    Before the normal scan, also runs a "stuck sweep" that transitions
    issues stuck in `<dispatch>-working` for longer than 30 minutes back
    to an error state. Self-heals tick crashes that left labels orphaned.
    """
    checks = run_doctor(config)
    failed = [c for c in checks if not c.ok]
    if failed:
        # Include each failing check's detail so Railway logs surface what
        # actually went wrong (e.g. "$GITHUB_TOKEN not set" vs a JWT mint
        # error vs gh auth status's exit message). Without this we lose
        # the diagnostic on a one-shot cron container.
        details = "; ".join(f"{c.name}: {c.detail}" for c in failed)
        print(
            f"alchemist: doctor failed; skipping tick — {details}",
            file=sys.stderr,
        )
        return [_run_level_error(config, f"doctor: {details}")]

    sweep_results = _sweep_stuck(config)
    remaining_issue_budget = max(0, config.max_issues_per_tick - len(sweep_results))

    try:
        issues = scan(org=config.org)
    except Exception as exc:  # noqa: BLE001 — surface any scanner failure to operator
        print(f"alchemist: scan failed: {exc}", file=sys.stderr)
        return [*sweep_results, _run_level_error(config, f"scan: {exc}")]

    state_labels = {
        label.lower()
        for label in _state_labels(config.dispatch_label)
        if label != config.dispatch_label
    }
    ignored_labels = {*state_labels, _SKIP_LABEL}
    issues = [
        i for i in issues
        if {label.lower() for label in i.labels}.isdisjoint(ignored_labels)
    ]

    if config.repo_blocklist:
        skipped = [i for i in issues if i.repository in config.repo_blocklist]
        if skipped:
            for i in skipped:
                print(
                    f"alchemist: skipping {i.repository}#{i.number} (repo in blocklist)",
                    file=sys.stderr,
                )
        issues = [i for i in issues if i.repository not in config.repo_blocklist]

    work = _select_work(issues, config, limit=remaining_issue_budget)

    results: list[RunResult] = list(sweep_results)
    if work:
        workers = max(1, min(config.max_concurrent_repos, len(work)))
        if workers == 1:
            for repo, slice_ in work:
                results.extend(_process_repo(repo, slice_, config))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_process_repo, repo, slice_, config)
                    for repo, slice_ in work
                ]
                results.extend([r for fut in futures for r in fut.result()])

    _self_file_failures(results, config)
    return results


def _process_repo(
    repo: str, issues: list[DispatchIssue], config: Config
) -> list[RunResult]:
    """Process up to `max_per_repo_per_tick` issues for one repo, serialized
    behind a per-repo lock."""
    if not issues:
        return []

    if config.dry_run:
        print(f"[DRY-RUN] would ensure labels on {repo}", file=sys.stderr)
    else:
        try:
            _ensure_labels(repo, config.dispatch_label)
        except _GhError as exc:
            return [
                RunResult(
                    repo=repo,
                    issue_number=i.number,
                    pr_url=None,
                    merged=None,
                    error=f"label-ensure: {exc}",
                    elapsed_sec=0.0,
                    dry_run=config.dry_run,
                )
                for i in issues
            ]

    note = f"{len(issues)} issue(s); first=#{issues[0].number}"
    try:
        with acquire(
            config.state_dir,
            repo,
            holder_note=note,
            stale_after_sec=_stuck_sweep_threshold_sec(config),
        ):
            return [_process_issue(issue, config) for issue in issues]
    except LockBusyError as exc:
        return [
            RunResult(
                repo=repo,
                issue_number=i.number,
                pr_url=None,
                merged=None,
                error=f"lock-busy: {exc}",
                elapsed_sec=0.0,
                dry_run=config.dry_run,
            )
            for i in issues
        ]


def _select_work(
    issues: list[DispatchIssue], config: Config, *, limit: int
) -> list[tuple[str, list[DispatchIssue]]]:
    """Select bounded work for this tick, preserving oldest-issue-first order.

    Invariant: the returned issue count never exceeds the global tick limit,
    and no repo receives more than `max_per_repo_per_tick`.
    """
    total_limit = max(0, limit)
    per_repo_limit = max(0, config.max_per_repo_per_tick)
    if total_limit == 0 or per_repo_limit == 0:
        return []

    selected: dict[str, list[DispatchIssue]] = {}
    per_repo_counts: dict[str, int] = defaultdict(int)
    selected_count = 0
    for issue in sorted(issues, key=lambda i: (i.updated_at, i.repository, i.number)):
        if selected_count >= total_limit:
            break
        if per_repo_counts[issue.repository] >= per_repo_limit:
            continue
        selected.setdefault(issue.repository, []).append(issue)
        per_repo_counts[issue.repository] += 1
        selected_count += 1
    return list(selected.items())


def _stuck_sweep_threshold_sec(config: Config) -> int:
    return max(
        _MIN_STUCK_SWEEP_THRESHOLD_SEC,
        config.conductor_timeout_sec + config.review_timeout_sec + _STUCK_SWEEP_MARGIN_SEC,
    )


def _process_issue(issue: DispatchIssue, config: Config) -> RunResult:
    started = time.monotonic()
    try:
        return _process_locked(issue, config, started)
    except Exception as exc:  # noqa: BLE001 — every per-issue failure is recoverable
        message = f"unhandled: {_sanitize_error_text(str(exc), token=config.github_token)}"
        if not config.dry_run:
            try:
                return _bail(issue.repository, issue, started, config, message)
            except Exception as bail_exc:  # noqa: BLE001 — preserve the original failure
                message = (
                    f"{message}; bail-failed: "
                    f"{_sanitize_error_text(str(bail_exc), token=config.github_token)}"
                )
        return RunResult(
            repo=issue.repository,
            issue_number=issue.number,
            pr_url=None,
            merged=None,
            error=message,
            elapsed_sec=time.monotonic() - started,
            dry_run=config.dry_run,
        )


def _process_locked(
    issue: DispatchIssue, config: Config, started: float
) -> RunResult:
    repo = issue.repository
    token = config.github_token
    if not token:
        return _result(repo, issue.number, started, config, error="missing GITHUB_TOKEN")

    if not config.dry_run:
        try:
            _set_label(repo, issue.number, _working_label(config.dispatch_label), config)
        except _GhError as exc:
            return _result(repo, issue.number, started, config, error=f"label-transition: {exc}")
        _post_activity_comment(repo, issue.number, _pickup_comment_body(), config)
        if _should_set_assignee(config):
            try:
                _set_assignee(repo, issue.number, "add", config.assignee_user, config)
            except _GhError as exc:
                print(
                    f"alchemist: warning — could not assign {config.assignee_user}: {exc}",
                    file=sys.stderr,
                )

    try:
        conductor_timeout_sec = _conductor_timeout_for_issue(issue, config)
    except ValueError as exc:
        return _bail(repo, issue, started, config, f"conductor-timeout: {exc}")

    try:
        default_branch = _default_branch(repo)
    except _GhError as exc:
        return _bail(repo, issue, started, config, f"default-branch: {exc}")

    work_dir = config.state_dir / "work" / f"{repo.replace('/', '-')}-{issue.number}"
    try:
        _clone_or_update(repo, work_dir, default_branch, token)
    except subprocess.SubprocessError as exc:
        return _bail(repo, issue, started, config, f"clone: {exc}")

    branch = _branch_name(issue)
    try:
        _make_branch(work_dir, branch, default_branch)
    except subprocess.SubprocessError as exc:
        return _bail(repo, issue, started, config, f"branch: {exc}")

    try:
        _prepare_target_repo(work_dir)
    except _ToolError as exc:
        report_tool_failure(
            config, "target-dependencies", str(exc), repo_context=repo, issue_number=issue.number
        )
        return _bail(repo, issue, started, config, f"dependency-prepare: {exc}")

    brief_path = config.state_dir / "briefs" / f"{repo.replace('/', '-')}-{issue.number}.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(render_brief(issue, repo, work_dir))

    transcript_path = (
        config.state_dir / "transcripts" / f"{repo.replace('/', '-')}-{issue.number}.log"
    )
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    ndjson_path = transcript_path.with_suffix(".ndjson")

    try:
        _run_conductor(
            brief_path=brief_path,
            cwd=work_dir,
            provider=config.default_provider,
            timeout=conductor_timeout_sec,
            transcript_path=transcript_path,
            ndjson_path=ndjson_path,
        )
    except _ToolError as exc:
        report_tool_failure(
            config, "conductor", str(exc), repo_context=repo, issue_number=issue.number
        )
        return _bail(repo, issue, started, config, f"conductor: {exc}")

    budget_problem = _check_budget(ndjson_path, config.default_budget)
    if budget_problem:
        return _bail(repo, issue, started, config, f"budget-exceeded: {budget_problem}")

    if not _has_changes(work_dir):
        return _decline(
            repo,
            issue,
            started,
            config,
            "conductor produced no diff",
        )

    diff_problem = _validate_diff(work_dir)
    if diff_problem:
        # Conductor produced a diff but it doesn't parse cleanly. Bail before
        # staging/pushing so we never open a PR with corrupted output.
        return _bail(repo, issue, started, config, f"diff-validate: {diff_problem}")

    # If the target repo has a .cortex/ directory, append a journal entry
    # before staging so it ships in the same PR as the diff. Best-effort.
    agent_summary = _extract_agent_summary(transcript_path)
    _maybe_write_cortex_journal(
        work_dir, repo, issue, branch, config.default_provider, agent_summary,
    )

    if config.dry_run:
        msg = (
            f"[DRY-RUN] {repo}#{issue.number}: would commit, push branch "
            f"{branch}, open PR, and call merge-pr.sh"
        )
        print(msg, file=sys.stderr)
        return _result(repo, issue.number, started, config)

    try:
        _stage_and_commit(work_dir, f"alchemist: {issue.title}")
    except subprocess.SubprocessError as exc:
        return _bail(repo, issue, started, config, f"commit: {exc}")

    try:
        _push_branch(work_dir, branch, repo, token)
    except subprocess.SubprocessError as exc:
        return _bail(repo, issue, started, config, f"push: {exc}", branch=branch)

    body = render_pr_body(
        issue=issue,
        provider=config.default_provider,
        agent_summary=agent_summary,
    )
    pr_title = f"{_PR_TITLE_PREFIX} fix: {issue.title} (#{issue.number})"
    try:
        pr_url, pr_number = _make_pr(repo, default_branch, branch, pr_title, body)
    except _GhError as exc:
        return _bail(repo, issue, started, config, f"pr-create: {exc}", branch=branch)

    # Touchstone owns the review-and-merge gate. Alchemist hands off the PR
    # number and waits for the result. CLEAN review → squash-merged. BLOCKED
    # review → PR stays open with review comments; needs human triage.
    try:
        merge_gate = _run_merge_pr(work_dir, pr_number, config.review_timeout_sec)
    except _ToolError as exc:
        # merge-pr.sh subprocess died; the merge may have already landed.
        # Query the PR's actual state before reporting failure.
        if _check_pr_merged(repo, pr_number):
            merged = True
        else:
            # Couldn't run/finish merge-pr.sh and can't confirm merged state.
            # Keep the PR URL so a human can pick up triage if needed.
            return _result(
                repo, issue.number, started, config,
                pr_url=pr_url, merged=False, error=f"merge-pr: {exc}",
            )
    else:
        merged = merge_gate.merged
        if merge_gate.error:
            label_error = _mark_merge_gate_error(repo, issue, pr_url, config, merge_gate.error)
            return _result(
                repo,
                issue.number,
                started,
                config,
                pr_url=pr_url,
                merged=False,
                error=label_error,
            )

    if merged:
        _post_activity_comment(
            repo,
            issue.number,
            f"✅ alchemist shipped — see {pr_url}",
            config,
        )
        shipped_label_error = None
        try:
            _set_label(repo, issue.number, _shipped_label(config.dispatch_label), config)
        except _GhError as exc:
            shipped_label_error = f"label-transition: {exc}"
            print(f"alchemist: {repo}#{issue.number}: {shipped_label_error}", file=sys.stderr)
        return _result(
            repo,
            issue.number,
            started,
            config,
            pr_url=pr_url,
            merged=True,
            error=shipped_label_error,
        )

    blocked_label_error = None
    _post_activity_comment(
        repo,
        issue.number,
        (
            f"⏸ alchemist opened {pr_url} but Touchstone blocked the merge.\n"
            "Awaiting human triage. Remove the `alchemist-blocked` label after\n"
            "you address the review."
        ),
        config,
    )
    try:
        _set_label(repo, issue.number, _blocked_label(config.dispatch_label), config)
    except _GhError as exc:
        blocked_label_error = f"label-transition: {exc}"
        print(f"alchemist: {repo}#{issue.number}: {blocked_label_error}", file=sys.stderr)

    return _result(
        repo,
        issue.number,
        started,
        config,
        pr_url=pr_url,
        merged=False,
        error=blocked_label_error,
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


class _GhError(RuntimeError):
    pass


class _ToolError(RuntimeError):
    pass


class _SanitizedSubprocessError(subprocess.SubprocessError):
    pass


class _SanitizedSubprocessTimeoutError(_SanitizedSubprocessError):
    pass


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_AUTH_HEADER_RE = re.compile(r"Authorization:\s*Basic\s+[A-Za-z0-9+/=]+", re.IGNORECASE)
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
_CONDUCTOR_TIMEOUT_OVERRIDE_RE = re.compile(
    r"(?im)^\s*alchemist-(?:conductor-)?timeout\s*:\s*(?P<value>\S.*?)\s*$"
)
_CONDUCTOR_TIMEOUT_MIN_SEC = 60
_CONDUCTOR_TIMEOUT_MAX_SEC = 60 * 60
_META_REPO = "autumngarage/alchemist"
_META_TITLE_PREFIX = "[alchemist-meta] failure: "
_META_SIGNATURE_LEN = 12
_EXTERNAL_FAILURE_PATTERNS = (
    re.compile(r"\brate.?limit\b", re.IGNORECASE),
    re.compile(r"\b(?:429|503|504)\b", re.IGNORECASE),
    re.compile(r"\beconnrefused\b", re.IGNORECASE),
    re.compile(r"\bnetwork\b", re.IGNORECASE),
    re.compile(r"github api", re.IGNORECASE),
)


def _branch_name(issue: DispatchIssue) -> str:
    slug = _SLUG_RE.sub("-", issue.title.lower()).strip("-")[:40]
    return f"alchemist/issue-{issue.number}-{slug}" if slug else f"alchemist/issue-{issue.number}"


def _conductor_timeout_for_issue(issue: DispatchIssue, config: Config) -> int:
    override = _parse_conductor_timeout_override(issue.body)
    return config.conductor_timeout_sec if override is None else override


def _parse_conductor_timeout_override(body: str) -> int | None:
    match = _CONDUCTOR_TIMEOUT_OVERRIDE_RE.search(body)
    if not match:
        return None
    seconds = _parse_duration_seconds(match.group("value"))
    if not (_CONDUCTOR_TIMEOUT_MIN_SEC <= seconds <= _CONDUCTOR_TIMEOUT_MAX_SEC):
        raise ValueError(
            "timeout must be between "
            f"{_CONDUCTOR_TIMEOUT_MIN_SEC}s and {_CONDUCTOR_TIMEOUT_MAX_SEC}s"
        )
    return seconds


def _parse_duration_seconds(raw: str) -> int:
    cleaned = raw.strip().lower()
    match = re.fullmatch(r"(?P<amount>\d+)\s*(?P<unit>[a-z]+)?", cleaned)
    if not match:
        raise ValueError(
            "expected duration like '1500s', '25m', or '1h'"
        )

    amount = int(match.group("amount"))
    unit = match.group("unit") or "s"
    multipliers = {
        "s": 1,
        "sec": 1,
        "secs": 1,
        "second": 1,
        "seconds": 1,
        "m": 60,
        "min": 60,
        "mins": 60,
        "minute": 60,
        "minutes": 60,
        "h": 60 * 60,
        "hr": 60 * 60,
        "hrs": 60 * 60,
        "hour": 60 * 60,
        "hours": 60 * 60,
    }
    multiplier = multipliers.get(unit)
    if multiplier is None:
        raise ValueError(
            "expected duration unit s, m, or h"
        )
    return amount * multiplier


def _pickup_comment_body() -> str:
    started = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return (
        "🧪 alchemist picked this up.\n\n"
        "- Worker: autumn-alchemist[bot]\n"
        f"- Started: {started}\n"
        "- Next: Conductor agent runs, then a PR opens "
        "(linked here automatically via \"Closes #N\")\n\n"
        "If you want alchemist to drop this, remove the `alchemist-working` label\n"
        "or close the issue. Alchemist will not pick the issue up again on its own."
    )


def _label_prefix(dispatch: str) -> str:
    return dispatch.removesuffix("-dispatch")


def _derived_label(dispatch: str, suffix: str) -> str:
    return f"{_label_prefix(dispatch)}-{suffix}"


def _working_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for the dispatch → working transition."""
    return dispatch, _derived_label(dispatch, "working")


def _blocked_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for working → blocked."""
    return _working_label(dispatch)[1], _derived_label(dispatch, "blocked")


def _shipped_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for working → shipped."""
    return _working_label(dispatch)[1], _derived_label(dispatch, "shipped")


def _declined_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for any → declined."""
    return _working_label(dispatch)[1], _derived_label(dispatch, "declined")


def _error_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for any → error."""
    return _working_label(dispatch)[1], _derived_label(dispatch, "error")


def _state_labels(dispatch_label: str) -> tuple[str, ...]:
    """All labels in Alchemist's state machine for one dispatch label."""
    return (
        dispatch_label,
        _working_label(dispatch_label)[1],
        _blocked_label(dispatch_label)[1],
        _shipped_label(dispatch_label)[1],
        _declined_label(dispatch_label)[1],
        _error_label(dispatch_label)[1],
    )


_LABEL_PALETTE: tuple[tuple[str, str, str], ...] = (
    # (suffix-key, color, description) for the state-machine labels.
    ("working",  "fff5d7", "Alchemist actively working"),
    ("blocked",  "cfd7ff", "Alchemist PR blocked by Touchstone review"),
    ("shipped",  "d7ffd7", "Alchemist shipped a PR"),
    ("declined", "d7d7ff", "Alchemist reviewed and declined to make changes"),
    ("error",    "ffd7d7", "Alchemist hit an error"),
)


def _expected_labels(dispatch_label: str) -> dict[str, tuple[str, str]]:
    """Return {label_name: (color, description)} for the labels alchemist
    needs on every watched repo."""
    names = {
        "working": _working_label(dispatch_label)[1],
        "blocked": _blocked_label(dispatch_label)[1],
        "shipped": _shipped_label(dispatch_label)[1],
        "declined": _declined_label(dispatch_label)[1],
        "error": _error_label(dispatch_label)[1],
    }
    return {
        names[key]: (color, desc)
        for key, color, desc in _LABEL_PALETTE
    }


def _ensure_labels(repo: str, dispatch_label: str) -> None:
    """Idempotently create alchemist's expected state-label set on `repo`.

    Removes the manual-setup cliff for new operators (alchemist#19): alchemist
    transitions among `-working`, `-blocked`, `-shipped`, `-declined`, and
    `-error`. These labels must exist on the target repo or `gh issue edit
    --add-label` silently fails.

    `gh label create --force` is idempotent at the gh level: if the label
    already exists with the same color/description, it's a no-op; if it
    differs, gh updates it. Either way alchemist gets to the state it needs.
    Cached per-process so the cron tick doesn't pay the round-trip every
    time once the labels are in place.
    """
    cache_key = (repo, dispatch_label)
    if cache_key in _LABELS_ENSURED:
        return

    for name, (color, desc) in _expected_labels(dispatch_label).items():
        cmd = [
            "gh", "label", "create", name,
            "--repo", repo,
            "--color", color,
            "--description", desc,
            "--force",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # noqa: S603
        if result.returncode != 0:
            raise _GhError(
                f"could not ensure label {name!r} on {repo}: "
                f"{result.stderr.strip() or f'gh label create exit {result.returncode}'}"
            )

    _LABELS_ENSURED.add(cache_key)


def _sweep_stuck(config: Config) -> list[RunResult]:
    """Find issues stuck in `<dispatch>-working` longer than the threshold and
    transition them to `<dispatch>-error`. Self-heals tick crashes that left
    labels mid-transition.

    Skipped in dry-run (mutating).

    Race window: if an active worker is just finishing, the sweep could
    transition to error while the worker overwrites with shipped. The two
    transitions are independent gh API calls; whichever lands last wins.
    Acceptable for autumn-garage's scale.
    """
    if config.dry_run:
        return []

    working_label = _working_label(config.dispatch_label)[1]
    cmd = [
        "gh", "search", "issues",
        "--owner", config.org,
        "--label", working_label,
        "--state", "open",
        "--json", "number,title,repository,updatedAt",
    ]
    try:
        result = subprocess.run(  # noqa: S603,S607
            cmd, capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"alchemist: stuck-sweep search failed: {exc}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(
            f"alchemist: stuck-sweep search exit {result.returncode}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return []
    try:
        items = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        print(f"alchemist: stuck-sweep parse failed: {exc}", file=sys.stderr)
        return []

    now = datetime.now(UTC)
    stale_after_sec = _stuck_sweep_threshold_sec(config)
    threshold = timedelta(seconds=stale_after_sec)
    swept: list[RunResult] = []
    sweep_limit = max(0, config.max_issues_per_tick)
    for item in items:
        if len(swept) >= sweep_limit:
            break
        repo_obj = item.get("repository") or {}
        repo = repo_obj.get("nameWithOwner") or repo_obj.get("name") or ""
        issue_num = item.get("number")
        updated_str = item.get("updatedAt", "")
        if not (repo and issue_num and updated_str):
            continue
        if repo in config.repo_blocklist:
            print(
                f"alchemist: stuck-sweep skipping {repo}#{issue_num} (repo in blocklist)",
                file=sys.stderr,
            )
            continue
        if is_locked(config.state_dir, repo, stale_after_sec=stale_after_sec):
            print(
                f"alchemist: stuck-sweep skipping {repo}#{issue_num} (active repo lock)",
                file=sys.stderr,
            )
            continue
        try:
            updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        age = now - updated
        if age < threshold:
            continue

        branch = _branch_name(
            DispatchIssue(
                number=issue_num,
                title=item.get("title") or "",
                body="",
                url=f"https://github.com/{repo}/issues/{issue_num}",
                repository=repo,
                updated_at=updated_str,
                labels=(working_label,),
            )
        )
        pr = _pr_state_for_head(repo, branch)
        if pr is not None and pr["mergedAt"] is not None:
            pr_url = pr["url"]
            _post_activity_comment(
                repo,
                issue_num,
                f"alchemist: detected merged PR {pr_url}; transitioning to shipped",
                config,
            )
            label_error = None
            try:
                _set_label(repo, issue_num, _shipped_label(config.dispatch_label), config)
            except _GhError as exc:
                label_error = f"label-transition: {exc}"
                print(f"alchemist: {repo}#{issue_num}: {label_error}", file=sys.stderr)

            swept.append(
                RunResult(
                    repo=repo,
                    issue_number=issue_num,
                    pr_url=pr_url,
                    merged=True,
                    error=label_error,
                    elapsed_sec=0.0,
                    dry_run=config.dry_run,
                )
            )
            continue
        if pr is not None and pr["state"] == "OPEN":
            print(
                f"alchemist: stuck-sweep skipping {repo}#{issue_num}: PR {pr['url']} still open",
                file=sys.stderr,
            )
            continue

        message = (
            f"detected stuck `-working` state ({age.total_seconds() / 60:.0f} min old); "
            "transitioning to error"
        )
        _post_activity_comment(
            repo, issue_num,
            f"alchemist: {message}\n\n"
            "A previous tick exited without completing the label transition. "
            "Inspect any outstanding branches before retrying.",
            config,
        )
        label_error = None
        try:
            _set_label(repo, issue_num, _error_label(config.dispatch_label), config)
        except _GhError as exc:
            label_error = f"label-transition: {exc}"
            print(f"alchemist: {repo}#{issue_num}: {label_error}", file=sys.stderr)

        swept.append(
            RunResult(
                repo=repo,
                issue_number=issue_num,
                pr_url=None,
                merged=None,
                error=(
                    f"stuck-sweep: {message}; {label_error}"
                    if label_error
                    else f"stuck-sweep: {message}"
                ),
                elapsed_sec=0.0,
                dry_run=config.dry_run,
            )
        )
    return swept


def _set_label(repo: str, issue_number: int, transition: tuple[str, str], config: Config) -> None:
    if config.dry_run:
        return
    try:
        _set_label_once(repo, issue_number, transition, config)
    except _GhError:
        time.sleep(1.5)
        _set_label_once(repo, issue_number, transition, config)


def _set_label_once(
    repo: str, issue_number: int, transition: tuple[str, str], config: Config
) -> None:
    _remove, add = transition
    current_state_labels = _current_state_labels(repo, issue_number, config.dispatch_label)
    to_remove = sorted(label for label in current_state_labels if label != add)
    cmd = [
        "gh", "issue", "edit", str(issue_number),
        "--repo", repo,
    ]
    if to_remove:
        cmd.extend(["--remove-label", ",".join(to_remove)])
    if add not in current_state_labels:
        cmd.extend(["--add-label", add])
    if len(cmd) == 6:
        return
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    if result.returncode != 0:
        raise _GhError(result.stderr.strip() or f"gh issue edit exit {result.returncode}")


def _current_state_labels(repo: str, issue_number: int, dispatch_label: str) -> set[str]:
    expected = set(_state_labels(dispatch_label))
    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "labels",
        "--jq", ".labels[].name",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    if result.returncode != 0:
        raise _GhError(result.stderr.strip() or f"gh issue view labels exit {result.returncode}")
    return {label.strip() for label in result.stdout.splitlines() if label.strip() in expected}


def _default_branch(repo: str) -> str:
    cmd = [
        "gh", "repo", "view", repo,
        "--json", "defaultBranchRef",
        "--jq", ".defaultBranchRef.name",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    if result.returncode != 0:
        raise _GhError(result.stderr.strip() or f"gh repo view exit {result.returncode}")
    branch = result.stdout.strip()
    if not branch:
        raise _GhError("default branch returned empty")
    return branch


def _git_auth_prefix(token: str) -> list[str]:
    """Return a `git -c http.extraheader=...` prefix that authenticates without
    embedding the token in URLs.

    URL-embedded auth (`https://x-access-token:<token>@github.com/...`) leaks
    the token into:
      - git's stdout/stderr on clone, fetch, and `push --set-upstream`
        (which Railway captures into deployment logs)
      - the cloned repo's `.git/config` (where set-url persists it)
      - any error message that includes the URL
    With `http.extraheader`, the auth ride-alongs only the in-process git
    invocation. The token never appears in stored config or printed URLs.

    Uses HTTP Basic auth with `x-access-token:<token>` as the credential
    pair. GitHub's git-over-HTTPS endpoint accepts Basic for both
    classic/fine-grained PATs and App installation tokens; Bearer is
    accepted for the API but not for git operations on installation
    tokens. Basic works for everything.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["git", "-c", f"http.extraheader=Authorization: Basic {encoded}"]


def _sanitize_error_text(text: str, *, token: str | None = None) -> str:
    """Redact auth material before writing logs, issue comments, or RunResult."""
    sanitized = text
    if token:
        sanitized = sanitized.replace(token, "[redacted-token]")
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        sanitized = sanitized.replace(encoded, "[redacted-git-basic-auth]")
    sanitized = _AUTH_HEADER_RE.sub("Authorization: Basic [redacted]", sanitized)
    sanitized = re.sub(r"x-access-token:[^@\s]+", "x-access-token:[redacted]", sanitized)
    return _GITHUB_TOKEN_RE.sub("[redacted-token]", sanitized)


def _tail_text(path: Path, *, max_lines: int = 30, max_chars: int = 3000) -> str:
    """Return a bounded tail from a text file, or "" when missing/empty."""
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    tail = "\n".join(text.splitlines()[-max_lines:]).strip()
    if not tail:
        return ""
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def _split_conductor_tail(message: str) -> tuple[str, str | None]:
    marker = "transcript tail:\n"
    if marker not in message:
        return message, None

    head, remainder = message.split(marker, 1)
    tail_text = remainder.strip()
    reason = head.rstrip()

    if "\nsee " in remainder:
        tail_text, pointer = remainder.rsplit("\nsee ", 1)
        pointer = pointer.strip()
        reason = f"{head.rstrip()} see {pointer}".strip()

    tail = tail_text.strip()
    return reason, (tail or None)


def _normalize_error_for_signature(error: str) -> str:
    normalized = _sanitize_error_text(error).lower()
    normalized = re.sub(r"/var/alchemist/state/transcripts/[^\s`]+", "<transcript>", normalized)
    normalized = re.sub(r"\balchemist/issue-\d+(?:-[a-z0-9-]+)?\b", "<branch>", normalized)
    normalized = re.sub(r"\b(?:pr|pull)\s*#?\d+\b", "<pr>", normalized)
    normalized = re.sub(r"/pull/\d+", "/pull/<n>", normalized)
    normalized = re.sub(r"#\d+", "#<n>", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _failure_signature(error: str) -> str:
    normalized = _normalize_error_for_signature(error)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:_META_SIGNATURE_LEN]


def _failure_summary(error: str) -> str:
    first_line = error.splitlines()[0].strip() if error.strip() else "unspecified failure"
    first_line = re.sub(r"\s*transcript tail:\s*$", "", first_line, flags=re.IGNORECASE)
    if len(first_line) > 80:
        return first_line[:80].rstrip()
    return first_line or "unspecified failure"


def _find_existing_meta_issue(signature: str) -> int | None:
    cmd = [
        "gh", "issue", "list",
        "--repo", _META_REPO,
        "--state", "open",
        "--search", _META_TITLE_PREFIX,
        "--json", "number,title",
        "--limit", "100",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    if result.returncode != 0:
        raise _GhError(result.stderr.strip() or f"gh issue list exit {result.returncode}")

    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise _GhError(f"gh issue list JSON parse failed: {exc}") from exc

    if not isinstance(payload, list):
        return None

    for item in payload:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        number = item.get("number")
        if not isinstance(title, str) or not isinstance(number, int):
            continue
        if title.startswith(_META_TITLE_PREFIX) and signature in title:
            return number
    return None


def _self_file_failure(result: RunResult, config: Config) -> None:
    if not result.error:
        return

    sanitized_error = _sanitize_error_text(result.error, token=config.github_token)
    signature = _failure_signature(sanitized_error)
    existing_issue = _find_existing_meta_issue(signature)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    source = f"{result.repo}#{result.issue_number}"

    if existing_issue is not None:
        body = f"+1 occurrence: {source} at {timestamp}"
        comment_cmd = [
            "gh", "issue", "comment", str(existing_issue),
            "--repo", _META_REPO,
            "--body", body,
        ]
        comment_result = subprocess.run(  # noqa: S603
            comment_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if comment_result.returncode != 0:
            raise _GhError(
                comment_result.stderr.strip()
                or f"gh issue comment exit {comment_result.returncode}"
            )
        return

    summary = _failure_summary(sanitized_error)
    title = f"{_META_TITLE_PREFIX}{summary} [{signature}]"
    body = (
        "This issue was opened automatically by alchemist's self-file path.\n\n"
        f"Source: {source}\n"
        f"Signature: `{signature}`\n\n"
        "Sanitized failure:\n\n"
        "```text\n"
        f"{sanitized_error}\n"
        "```\n"
    )
    create_cmd = [
        "gh", "issue", "create",
        "--repo", _META_REPO,
        "--title", title,
        "--body", body,
    ]
    create_result = subprocess.run(  # noqa: S603
        create_cmd, capture_output=True, text=True, timeout=30,
    )
    if create_result.returncode != 0:
        raise _GhError(
            create_result.stderr.strip()
            or f"gh issue create exit {create_result.returncode}"
        )


def _is_external_failure(error: str) -> bool:
    return any(pattern.search(error) for pattern in _EXTERNAL_FAILURE_PATTERNS)


def _is_meta_issue_number(issue_number: int) -> bool:
    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", _META_REPO,
        "--json", "title",
        "--jq", ".title",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    if result.returncode != 0:
        return False
    title = result.stdout.strip()
    return title.startswith(_META_TITLE_PREFIX)


def _is_meta_self_issue_result(result: RunResult) -> bool:
    if result.repo != _META_REPO:
        return False
    return _is_meta_issue_number(result.issue_number)


def _self_file_failures(results: list[RunResult], config: Config) -> None:
    if config.dry_run:
        return

    disable = os.environ.get("ALCHEMIST_DISABLE_SELF_FILE", "").strip().lower()
    if disable in {"1", "true"}:
        return

    filed = 0
    for result in results:
        if not result.error:
            continue

        sanitized_error = _sanitize_error_text(result.error, token=config.github_token)
        if _is_external_failure(sanitized_error):
            continue
        if _is_meta_self_issue_result(result):
            continue

        if filed >= 3:
            print(
                f"alchemist: self-file cap reached; skipping meta-issue for "
                f"{result.repo}#{result.issue_number}",
                file=sys.stderr,
            )
            continue

        try:
            _self_file_failure(result, config)
        except Exception as exc:  # noqa: BLE001 — self-file is best-effort
            print(
                "alchemist: self-file failed for "
                f"{result.repo}#{result.issue_number}: "
                f"{_sanitize_error_text(str(exc), token=config.github_token)}",
                file=sys.stderr,
            )
            continue

        filed += 1


def _run_git_auth(
    cmd: list[str], *, cwd: Path | None = None, token: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run an authenticated git command without leaking auth in exceptions."""
    try:
        result = subprocess.run(  # noqa: S603,S607
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _SanitizedSubprocessError(
            _sanitize_error_text(str(exc), token=token)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _SanitizedSubprocessTimeoutError(
            _sanitize_error_text(str(exc), token=token)
        ) from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"git exit {result.returncode}"
        raise _SanitizedSubprocessError(_sanitize_error_text(detail, token=token))
    return result


def _clone_or_update(repo: str, dest: Path, default_branch: str, token: str) -> None:
    url = f"https://github.com/{repo}.git"  # plain URL — auth via header
    git_auth = _git_auth_prefix(token)
    if dest.exists() and (dest / ".git").exists():
        # Ensure the remote URL on disk is the plain (no-creds) form. If a
        # previous version of alchemist set it with creds, rewrite it.
        subprocess.run(  # noqa: S603,S607
            ["git", "remote", "set-url", "origin", url],
            cwd=dest, check=True, timeout=30,
        )
        _run_git_auth(
            [*git_auth, "fetch", "origin", default_branch, "--depth", "50"],
            cwd=dest, token=token, timeout=120,
        )
        subprocess.run(  # noqa: S603,S607
            ["git", "reset", "--hard", f"origin/{default_branch}"],
            cwd=dest, check=True, timeout=30,
        )
        subprocess.run(  # noqa: S603,S607
            ["git", "clean", "-fdx"],
            cwd=dest, check=True, timeout=30,
        )
        return

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_git_auth(
        [*git_auth, "clone", "--depth", "50", "--branch", default_branch, url, str(dest)],
        token=token, timeout=300,
    )


def _make_branch(dest: Path, branch: str, base: str) -> None:
    subprocess.run(  # noqa: S603,S607
        ["git", "checkout", "-B", branch, base],
        cwd=dest, check=True, timeout=30,
    )


def _conductor_env() -> dict[str, str]:
    """Return the environment for the worker agent process.

    Conductor needs normal process context and provider credentials, but not
    Alchemist's GitHub/App credentials. The worker can edit files through its
    tools; it should not also inherit orchestrator tokens.
    """
    env = dict(os.environ)
    for key in list(env):
        if key in {"GITHUB_TOKEN", "GH_TOKEN"} or key.startswith("ALCHEMIST_"):
            env.pop(key, None)
    return env


def _prepare_target_repo(repo_dir: Path) -> None:
    """Install target-repo dependencies before the worker and merge gate run.

    Invariant: dependency preparation must leave the checkout clean. If a
    setup command changes tracked or untracked files, the run bails before
    Conductor edits or PR creation so dependency drift does not get shipped as
    an unrelated model-authored change.
    """
    cmd = _target_dependency_prepare_command(repo_dir)
    if cmd is None:
        return

    env = {
        **_conductor_env(),
        # Touchstone-generated setup.sh files honor this to skip slow/global
        # verifier tool installation while still syncing project deps.
        "TOUCHSTONE_SKIP_DEVTOOLS": "1",
    }
    try:
        result = subprocess.run(  # noqa: S603,S607
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=env,
            timeout=_TARGET_DEP_PREP_TIMEOUT_SEC,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _ToolError(f"{cmd[0]} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise _ToolError(
            f"{cmd[0]} timeout after {_TARGET_DEP_PREP_TIMEOUT_SEC}s"
        ) from exc

    if result.returncode != 0:
        output = f"{result.stdout or ''}{result.stderr or ''}"
        raise _ToolError(
            f"{' '.join(cmd)} exit {result.returncode}; "
            f"tail={_merge_output_tail(_sanitize_error_text(output))}"
        )

    dirty = _status_entries(repo_dir)
    if dirty:
        preview = ", ".join(dirty[:8])
        suffix = "" if len(dirty) <= 8 else f", +{len(dirty) - 8} more"
        raise _ToolError(f"{' '.join(cmd)} left checkout dirty: {preview}{suffix}")


def _target_dependency_prepare_command(repo_dir: Path) -> list[str] | None:
    if (repo_dir / "setup.sh").is_file():
        return ["bash", "setup.sh", "--deps-only"]
    if (repo_dir / "uv.lock").is_file():
        return ["uv", "sync", "--locked"]
    return None


def _status_entries(repo_dir: Path) -> list[str]:
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise _ToolError(f"git status failed after dependency prepare: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"git exit {result.returncode}"
        raise _ToolError(f"git status failed after dependency prepare: {detail}")

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _run_conductor(
    *,
    brief_path: Path,
    cwd: Path,
    provider: str,
    timeout: int,
    transcript_path: Path,
    ndjson_path: Path,
) -> None:
    """Run conductor exec; on success, conductor's edits are present in cwd.

    Stdout is streamed to the transcript file so an operator can `cat` it
    after the fact. Conductor's own --timeout flag is set in addition to
    subprocess timeout for belt-and-suspenders. The structured NDJSON event
    stream is written to ndjson_path so alchemist can parse cost_usd from
    `event=="usage"` records (alchemist#33).
    """
    cmd = [
        "conductor", "exec",
        "--with", provider,
        "--tools", "Read,Edit,Write,Bash",
        "--brief-file", str(brief_path),
        "--cwd", str(cwd),
        "--timeout", str(timeout),
        "--log-file", str(ndjson_path),
    ]
    with transcript_path.open("w") as fh:
        try:
            result = subprocess.run(  # noqa: S603,S607
                cmd,
                stdout=fh, stderr=subprocess.STDOUT,
                env=_conductor_env(),
                text=True,
                timeout=timeout + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _ToolError(f"timeout after {timeout + 30}s") from exc

    if result.returncode != 0:
        tail = _tail_text(transcript_path)
        if not tail:
            raise _ToolError(f"exit {result.returncode} (transcript missing or empty)")
        sanitized_tail = _sanitize_error_text(tail)
        raise _ToolError(
            f"exit {result.returncode}; transcript tail:\n"
            f"{sanitized_tail}\n"
            f"see {transcript_path}"
        )


def _has_changes(repo_dir: Path) -> bool:
    result = subprocess.run(  # noqa: S603,S607
        ["git", "status", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True, timeout=10,
    )
    return bool(result.stdout.strip())


def _maybe_write_cortex_journal(
    work_dir: Path,
    repo: str,
    issue: DispatchIssue,
    branch: str,
    provider: str,
    agent_summary: str | None,
) -> None:
    """If the target repo has a `.cortex/` directory, append a journal entry
    documenting this alchemist transmute cycle. Best-effort — failures log
    but don't fail the run.

    Per the Cortex protocol (section 6: custom triggers), tools may add
    project-specific entries. Alchemist's entry uses a `T1.6-alchemist`
    trigger marker (T1.6 is the sentinel-cycle trigger; alchemist runs are
    the analogous "agent did some work" event).

    The journal entry is written into the cloned repo's working dir, so
    `_stage_and_commit` picks it up and it ships in the same PR as the
    diff. No second push or commit-amend needed.
    """
    cortex_dir = work_dir / ".cortex"
    if not cortex_dir.is_dir():
        return
    journal_dir = cortex_dir / "journal"
    try:
        journal_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"alchemist: cortex journal mkdir failed: {exc}", file=sys.stderr)
        return

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    iso_now = datetime.now(UTC).isoformat()
    filename = f"{today}-alchemist-{issue.number}.md"
    path = journal_dir / filename

    summary_block = ""
    if agent_summary:
        # Indent each line by two spaces to keep it inside a Markdown
        # blockquote-like fence, in case the summary contains funky chars.
        summary_block = (
            "\n## Agent summary\n\n"
            "```\n"
            f"{agent_summary[:600]}\n"
            "```\n"
        )

    body = (
        f"---\n"
        f"title: alchemist transmuted issue #{issue.number}\n"
        f"date: {iso_now}\n"
        f"type: alchemist-cycle\n"
        f"trigger: T1.6-alchemist\n"
        f"issue: {issue.url}\n"
        f"branch: {branch}\n"
        f"provider: {provider}\n"
        f"brief_template: {BRIEF_TEMPLATE_VERSION}\n"
        f"---\n\n"
        f"# Alchemist transmuted issue #{issue.number}\n\n"
        f"## Issue\n\n"
        f"> {issue.title}\n\n"
        f"Source: {issue.url}\n\n"
        f"## Cycle\n\n"
        f"- Branch: `{branch}`\n"
        f"- Provider: `{provider}`\n"
        f"- Brief template: v{BRIEF_TEMPLATE_VERSION}\n"
        f"{summary_block}"
    )
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        print(f"alchemist: cortex journal write failed: {exc}", file=sys.stderr)


def _extract_agent_summary(transcript_path: Path, max_chars: int = 600) -> str | None:
    """Pull a short "what the agent did" summary from conductor's transcript.

    Heuristic: take the trailing N chars of the transcript. LLMs typically
    end their tool-use loop with a natural-language wrap-up; the tail catches
    that. If the tail is empty or only whitespace, return None and the PR
    body just omits the summary section.

    Capped at `max_chars` so PR bodies don't bloat. UTF-8 decode errors are
    replaced (best-effort): we'd rather have a slightly garbled summary than
    crash on a transcript byte sequence we can't read.
    """
    if not transcript_path.exists():
        return None
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    tail = text[-max_chars:].strip()
    return tail or None


def _parse_budget(raw: str) -> float | None:
    """Parse a budget string like "$2", "$1.50", or "0.25" → float USD.

    Returns None when the budget is empty, zero, or unparseable — meaning
    "no cap, fail open." The cap is opt-in: a deployment that wants no
    enforcement can set ALCHEMIST_BUDGET="" or "$0".
    """
    if not raw:
        return None
    cleaned = raw.strip().lstrip("$").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def _extract_total_cost(ndjson_path: Path) -> float | None:
    """Sum cost_usd across `event=="usage"` records in conductor's NDJSON.

    Returns None if the log file is missing or has no usage events — that
    state is "we don't know what it cost," distinct from "$0 spent." The
    caller treats None as fail-open (no enforcement).
    """
    if not ndjson_path.exists():
        return None
    total = 0.0
    seen_usage = False
    try:
        text = ndjson_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != "usage":
            continue
        cost = record.get("data", {}).get("cost_usd")
        if isinstance(cost, (int, float)):
            total += float(cost)
            seen_usage = True
    return total if seen_usage else None


def _check_budget(ndjson_path: Path, raw_budget: str) -> str | None:
    """Return a human-readable problem string if the run exceeded budget.

    Returns None when the run is within budget OR when enforcement is
    disabled (empty/zero budget) OR when we couldn't parse cost (fail-open).
    """
    cap = _parse_budget(raw_budget)
    if cap is None:
        return None
    spent = _extract_total_cost(ndjson_path)
    if spent is None:
        return None
    if spent <= cap:
        return None
    return f"${spent:.2f} spent vs ${cap:.2f} budgeted"


def _validate_diff(repo_dir: Path) -> str | None:
    """Sanity-check conductor's diff before staging.

    Catches model-corruption-style outputs (hallucinated training-data
    fragments, mojibake, broken syntax) at the alchemist boundary so we
    bail before opening a PR full of garbage. Tier 1 today: Python files
    only — `compile()` is fast and native, no external tools needed.

    Returns an error message describing the first failure encountered, or
    None if all changed files parse cleanly.
    """
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "diff", "--name-only", "--diff-filter=AM"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # Don't block on a transient git failure — let touchstone's review
        # be the gate if this stage can't run cleanly.
        return f"diff-list failed: {exc}"

    for path in result.stdout.splitlines():
        path = path.strip()
        if not path:
            continue
        full = repo_dir / path
        if not full.is_file():
            continue
        if path.endswith(".py"):
            try:
                source = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return f"{path}: read failed: {exc}"
            try:
                compile(source, full.as_posix(), "exec")
            except SyntaxError as exc:
                return f"{path}: SyntaxError on line {exc.lineno}: {exc.msg}"
    return None


def _stage_and_commit(repo_dir: Path, message: str) -> None:
    """Stage all changes and commit, signing as Alchemist for audit visibility."""
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": _GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": _GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": _GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": _GIT_AUTHOR_EMAIL,
    }
    subprocess.run(  # noqa: S603,S607
        ["git", "add", "-A"],
        cwd=repo_dir, env=git_env, check=True, timeout=30,
    )
    subprocess.run(  # noqa: S603,S607
        ["git", "commit", "-m", message],
        cwd=repo_dir, env=git_env, check=True, timeout=30,
    )


def _resolve_touchstone_root() -> Path:
    """Locate the touchstone install (the dir containing scripts/merge-pr.sh)."""
    env_root = os.environ.get("TOUCHSTONE_ROOT")
    if env_root:
        candidate = Path(env_root)
        if (candidate / "scripts" / "merge-pr.sh").exists():
            return candidate
        if (candidate / "libexec" / "scripts" / "merge-pr.sh").exists():
            return candidate / "libexec"

    try:
        result = subprocess.run(  # noqa: S603,S607
            ["brew", "--prefix", "touchstone"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            brew_root = Path(result.stdout.strip())
            if (brew_root / "libexec" / "scripts" / "merge-pr.sh").exists():
                return brew_root / "libexec"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    for fallback in (Path("/opt/touchstone"), Path("/opt/touchstone/libexec")):
        if (fallback / "scripts" / "merge-pr.sh").exists():
            return fallback
    raise _ToolError("touchstone scripts/merge-pr.sh not found")


def _run_merge_pr(repo_dir: Path, pr_number: int, timeout: int) -> _MergeGateResult:
    """Hand the PR to touchstone's merge-pr.sh (review + auto-merge gate).

    Returns a classified result:
        merged=True, error=None — touchstone reviewed CLEAN/FIXED and merged.
        merged=False, error=None — touchstone review BLOCKED; PR stays open
            with review comments for human triage.
        merged=False, error=str — merge-gate infrastructure/preflight failure.

    Raises:
        _ToolError — couldn't locate or invoke merge-pr.sh at all.
    """
    root = _resolve_touchstone_root()
    script = root / "scripts" / "merge-pr.sh"
    try:
        result = subprocess.run(  # noqa: S603
            ["bash", str(script), str(pr_number)],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ToolError(f"merge-pr.sh timeout after {timeout}s") from exc

    if result.returncode != 0:
        merged_output = f"{result.stdout or ''}{result.stderr or ''}"
        tail_lines = merged_output.splitlines()[-50:]
        print(f"merge-pr.sh exited {result.returncode}; tail of output:", file=sys.stderr)
        for line in tail_lines:
            print(f"  {line}", file=sys.stderr)
        problem = _classify_merge_pr_failure(merged_output)
        if problem is None:
            return _MergeGateResult(merged=False)
        return _MergeGateResult(
            merged=False,
            error=f"{problem}; exit={result.returncode}; tail={_merge_output_tail(merged_output)}",
        )

    return _MergeGateResult(merged=True)


def _classify_merge_pr_failure(output: str) -> str | None:
    """Return None for intentional review blocks; otherwise a fatal reason."""
    lower = output.lower()
    if "codex_review_blocked" in lower or "review blocked" in lower:
        return None
    if "push blocked" in lower and "conductor flagged issues" in lower:
        return None
    if "preflight" in lower:
        return "preflight failed"
    if "merge conflict" in lower or "not mergeable" in lower:
        return "merge conflict"
    if "gh:" in lower or "graphql" in lower or "api rate limit" in lower or "github api" in lower:
        return "github api failure"
    return "merge-pr.sh failed"


def _merge_output_tail(output: str, *, max_lines: int = 8, max_chars: int = 800) -> str:
    tail = "\n".join(output.splitlines()[-max_lines:]).strip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail or "(no output)"


def _check_pr_merged(repo: str, pr_number: int) -> bool:
    """Return True if the PR has actually been merged (post-timeout safety check)."""
    cmd = [
        "gh", "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "mergedAt",
        "--jq", ".mergedAt",
    ]
    try:
        result = subprocess.run(  # noqa: S603,S607
            cmd, capture_output=True, text=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    output = result.stdout.strip()
    # `gh ... --jq .mergedAt` returns the timestamp string when merged, or empty/null otherwise.
    return bool(output) and output not in ("null", "")


def _find_pr_for_head(repo: str, branch: str) -> tuple[str, int] | None:
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--head", branch,
        "--state", "all",
        "--json", "url,number,state",
        "--limit", "1",
    ]
    try:
        result = subprocess.run(  # noqa: S603,S607
            cmd, capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None

    first = payload[0]
    if not isinstance(first, dict):
        return None
    url = first.get("url")
    number = first.get("number")
    if not isinstance(url, str) or not isinstance(number, int):
        return None
    return url, number


class _PrHeadState(TypedDict):
    url: str
    number: int
    state: str
    mergedAt: str | None


def _pr_state_for_head(repo: str, branch: str) -> _PrHeadState | None:
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--head", branch,
        "--state", "all",
        "--json", "url,number,state,mergedAt",
        "--limit", "1",
    ]
    try:
        result = subprocess.run(  # noqa: S603,S607
            cmd, capture_output=True, text=True, timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None

    first = payload[0]
    if not isinstance(first, dict):
        return None
    url = first.get("url")
    number = first.get("number")
    state = first.get("state")
    merged_at = first.get("mergedAt")
    if not isinstance(url, str) or not isinstance(number, int) or not isinstance(state, str):
        return None
    if merged_at is not None and not isinstance(merged_at, str):
        return None
    return {
        "url": url,
        "number": number,
        "state": state,
        "mergedAt": merged_at,
    }


def _push_branch(repo_dir: Path, branch: str, repo: str, token: str) -> None:
    # Use 'origin' (set to a plain URL by _clone_or_update) plus the auth
    # header — never embed the token in the URL. See _git_auth_prefix.
    #
    # Stale remote branches are expected after retrying a crashed tick or
    # closed PR. Fetch the exact remote branch ref first, then lease against
    # that SHA. If no remote branch exists, push normally. This avoids the
    # fresh-clone `--force-with-lease` rejection from alchemist#59 without
    # deleting the previous recovery branch before the replacement push
    # succeeds.
    _ = repo  # unused; origin already points at the right remote
    git_auth = _git_auth_prefix(token)
    _refresh_remote_branch_ref(repo_dir, branch, token)
    remote_oid = _remote_branch_oid(repo_dir, branch)
    force_option = (
        [f"--force-with-lease=refs/heads/{branch}:{remote_oid}"]
        if remote_oid
        else []
    )
    push_cmd = [*git_auth, "push", "--set-upstream", *force_option, "origin", branch]
    try:
        _run_git_auth(push_cmd, cwd=repo_dir, token=token, timeout=120)
        return
    except _SanitizedSubprocessTimeoutError as exc:
        original_error = exc

    local_sha = _local_head_sha(repo_dir)
    remote_sha = _remote_branch_sha(repo_dir, branch, token)
    if local_sha and remote_sha == local_sha:
        print(
            f"alchemist: push timeout reconciled for {branch}; remote already at {local_sha}",
            file=sys.stderr,
        )
        return

    try:
        _run_git_auth(push_cmd, cwd=repo_dir, token=token, timeout=120)
    except subprocess.SubprocessError as exc:
        raise original_error from exc


def _refresh_remote_branch_ref(repo_dir: Path, branch: str, token: str) -> None:
    """Best-effort refresh of `origin/<branch>` for force-with-lease."""
    remote_ref = f"refs/remotes/origin/{branch}"
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603,S607
            ["git", "update-ref", "-d", remote_ref],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    git_auth = _git_auth_prefix(token)
    refspec = f"+refs/heads/{branch}:{remote_ref}"
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603,S607
            [*git_auth, "fetch", "origin", refspec, "--depth", "1"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


def _remote_branch_oid(repo_dir: Path, branch: str) -> str | None:
    remote_ref = f"refs/remotes/origin/{branch}"
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "rev-parse", "--verify", remote_ref],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    oid = result.stdout.strip()
    return oid or None


def _remote_branch_sha(repo_dir: Path, branch: str, token: str) -> str | None:
    git_auth = _git_auth_prefix(token)
    try:
        result = _run_git_auth(
            [*git_auth, "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=repo_dir,
            token=token,
            timeout=10,
        )
    except subprocess.SubprocessError:
        return None

    line = result.stdout.strip().splitlines()
    if not line:
        return None
    first = line[0].strip().split()
    if not first:
        return None
    sha = first[0]
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    return None


def _local_head_sha(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    return None


def _make_pr(
    repo: str, base: str, head: str, title: str, body: str
) -> tuple[str, int]:
    """Open a PR; return (url, number)."""
    cmd = [
        "gh", "pr", "create",
        "--repo", repo,
        "--base", base,
        "--head", head,
        "--title", title,
        "--body", body,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603
    except subprocess.TimeoutExpired as exc:
        reconciled = _find_pr_for_head(repo, head)
        if reconciled is not None:
            return reconciled
        raise _GhError("pr-create: timeout, no PR found on reconciliation") from exc
    if result.returncode != 0:
        raise _GhError(result.stderr.strip() or f"gh pr create exit {result.returncode}")
    url = result.stdout.strip().splitlines()[-1]
    # The URL ends in /pull/<N>; pull the number out so we can hand it to merge-pr.sh.
    match = re.search(r"/pull/(\d+)", url)
    if not match:
        raise _GhError(f"could not parse PR number from gh output: {url!r}")
    return url, int(match.group(1))


def _post_activity_comment(
    repo: str, issue_number: int, body: str, config: Config,
) -> None:
    """Post a comment on the issue describing alchemist's activity.

    Best-effort: failures (gh missing, timeout, non-zero exit) don't fail the
    run — the comment is a visibility aid, not a load-bearing primitive.
    Skipped in dry-run.
    """
    if config.dry_run:
        return
    body = _sanitize_error_text(body, token=config.github_token)
    cmd = [
        "gh", "issue", "comment", str(issue_number),
        "--repo", repo,
        "--body", body,
    ]
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603,S607
            cmd, capture_output=True, text=True, timeout=30, check=False,
        )


def _post_error_comment(
    repo: str,
    issue_number: int,
    message: str,
    config: Config,
    *,
    branch: str | None = None,
) -> None:
    """Post a uniform error outcome comment for bail/error transitions."""
    reason, tail = _split_conductor_tail(message)
    branch_text = branch or "(unknown)"
    details_block = ""
    if tail:
        details_block = (
            "\n<details>\n"
            "<summary>Conductor transcript tail</summary>\n\n"
            "```text\n"
            f"{tail}\n"
            "```\n\n"
            "</details>\n"
        )

    _post_activity_comment(
        repo,
        issue_number,
        (
            f"⚠️ alchemist hit an error: {reason}\n\n"
            f"Working branch: {branch_text}\n"
            f"{details_block}"
            "Inspect the branch or remove the `alchemist-error` label to retry."
        ),
        config,
    )


def _set_assignee(
    repo: str, issue_number: int, action: str, assignee: str, config: Config,
) -> None:
    """Add or remove an assignee. `action` is 'add' or 'remove'.

    Issue claiming (alchemist#23): when alchemist starts work on an issue, it
    assigns to itself so the audit trail is visible — operators glancing at
    the issue list see the claim, not just an `-working` label.

    v0.1 (PAT auth): assigns to the PAT owner (e.g. henrymodisett). Visible
    but not perfectly attributable to the bot.
    v0.2 (App auth, alchemist#6): swap to autumn-alchemist[bot]. Clean.

    Skipped in dry-run.
    """
    if config.dry_run:
        return
    flag = "--add-assignee" if action == "add" else "--remove-assignee"
    cmd = [
        "gh", "issue", "edit", str(issue_number),
        "--repo", repo,
        flag, assignee,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    if result.returncode != 0:
        raise _GhError(
            result.stderr.strip() or f"gh issue edit assignee {action} exit {result.returncode}"
        )


def _should_set_assignee(config: Config) -> bool:
    """Return whether this deployment can use GitHub's issue assignee API."""
    if not config.assignee_user.strip():
        return False
    return not config.has_app_credentials


def _mark_merge_gate_error(
    repo: str,
    issue: DispatchIssue,
    pr_url: str,
    config: Config,
    message: str,
) -> str:
    error = f"merge-pr: {message}"
    if not config.dry_run:
        _post_error_comment(
            repo,
            issue.number,
            f"{error}\n\nPR remains open for human triage: {pr_url}",
            config,
        )
        try:
            _set_label(repo, issue.number, _error_label(config.dispatch_label), config)
        except _GhError as exc:
            label_error = f"label-transition: {exc}"
            print(f"alchemist: {repo}#{issue.number}: {label_error}", file=sys.stderr)
            error = f"{error}; {label_error}"
    return error


def _bail(
    repo: str,
    issue: DispatchIssue,
    started: float,
    config: Config,
    message: str,
    *,
    branch: str | None = None,
) -> RunResult:
    """Common error path: post a comment, transition to error label, return result."""
    message = _sanitize_error_text(message, token=config.github_token)
    label_error: str | None = None
    visible_branch = branch if branch and message.startswith(("push:", "pr-create:")) else None
    if visible_branch and message.startswith("pr-create:"):
        reconciled = _find_pr_for_head(repo, visible_branch)
        if reconciled is not None:
            reconciled_url, reconciled_number = reconciled
            message = (
                f"{message}; reconciliation: found existing PR "
                f"{reconciled_url} (#{reconciled_number})"
            )
    if not config.dry_run:
        _post_error_comment(repo, issue.number, message, config, branch=visible_branch)
        try:
            _set_label(repo, issue.number, _error_label(config.dispatch_label), config)
        except _GhError as exc:
            label_error = f"label-transition: {exc}"
            print(f"alchemist: {repo}#{issue.number}: {label_error}", file=sys.stderr)
    error = f"{message}; {label_error}" if label_error else message
    return _result(repo, issue.number, started, config, error=error, branch=visible_branch)


def _decline(
    repo: str, issue: DispatchIssue, started: float, config: Config, message: str,
) -> RunResult:
    """Decline path: the LLM correctly judged the issue non-actionable."""
    label_error: str | None = None
    if not config.dry_run:
        _post_activity_comment(
            repo,
            issue.number,
            f"🚫 alchemist read this and declined: {message}",
            config,
        )
        try:
            _set_label(
                repo,
                issue.number,
                _declined_label(config.dispatch_label),
                config,
            )
        except _GhError as exc:
            label_error = f"label-transition: {exc}"
            print(f"alchemist: {repo}#{issue.number}: {label_error}", file=sys.stderr)
    if label_error:
        return _result(
            repo,
            issue.number,
            started,
            config,
            error=f"{label_error}; decline: {message}",
        )
    return _result(repo, issue.number, started, config, error=message)


def _result(
    repo: str,
    issue_number: int,
    started: float,
    config: Config,
    *,
    pr_url: str | None = None,
    merged: bool | None = None,
    error: str | None = None,
    branch: str | None = None,
) -> RunResult:
    return RunResult(
        repo=repo,
        issue_number=issue_number,
        pr_url=pr_url,
        merged=merged,
        error=error,
        elapsed_sec=time.monotonic() - started,
        dry_run=config.dry_run,
        branch=branch,
    )


def _run_level_error(config: Config, message: str) -> RunResult:
    return RunResult(
        repo=config.org,
        issue_number=0,
        pr_url=None,
        merged=None,
        error=_sanitize_error_text(message, token=config.github_token),
        elapsed_sec=0.0,
        dry_run=config.dry_run,
    )


__all__ = [
    "BRIEF_TEMPLATE_VERSION",
    "RunResult",
    "run_tick",
]
