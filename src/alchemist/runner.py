"""One Alchemist tick: dispatch labelled issues to an external coding agent.

Alchemist intentionally does not clone repositories, run an agent locally,
commit changes, or open pull requests. It owns only the GitHub coordination
layer around Codex/Devin:

    scan labelled issues -> claim/dispatch -> find linked PR -> nudge or merge

The external agent owns implementation and PR creation. GitHub remains the
source of truth for state.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from alchemist.doctor import run_doctor
from alchemist.locks import LockBusyError, acquire
from alchemist.scanner import DispatchIssue, scan

if TYPE_CHECKING:
    from alchemist.config import Config


_SKIP_LABEL = "alchemist-skip"
_LABELS_ENSURED: set[tuple[str, str, str]] = set()
_COMMENT_TIMEOUT_SEC = 30
_DEVIN_API_BASE = "https://api.devin.ai/v3"
_DISPATCH_MARKER = "<!-- alchemist-dispatch"
_NUDGE_MARKER = "<!-- alchemist-nudge:"
_AUTH_HEADER_RE = re.compile(r"Authorization:\s*Basic\s+[A-Za-z0-9+/=]+", re.IGNORECASE)
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
_DEVIN_TOKEN_RE = re.compile(r"\bcog_[A-Za-z0-9_-]+\b")


@dataclass(frozen=True)
class RunResult:
    repo: str
    issue_number: int
    pr_url: str | None
    merged: bool | None
    error: str | None
    elapsed_sec: float
    dry_run: bool
    branch: str | None = None
    status: str = "unknown"


@dataclass(frozen=True)
class _AgentDispatch:
    provider: str
    session_url: str | None = None
    session_id: str | None = None


class _GhError(RuntimeError):
    pass


class _AgentDispatchError(RuntimeError):
    pass


class _AgentFollowupError(RuntimeError):
    pass


class _PrState(TypedDict):
    url: str
    number: int
    state: str
    mergedAt: str | None
    reviewDecision: str | None
    isDraft: bool
    statusCheckRollup: list[dict[str, Any]]
    comments: list[dict[str, Any]]


def run_tick(config: Config) -> list[RunResult]:
    """Process one scheduler tick."""
    tick_started = time.monotonic()
    checks = run_doctor(config)
    failed = [c for c in checks if not c.ok]
    if failed:
        details = "; ".join(f"{c.name}: {c.detail}" for c in failed)
        print(f"alchemist: doctor failed; skipping tick — {details}", file=sys.stderr)
        results = [_run_level_error(config, f"doctor: {details}")]
        _log_tick_summary(results, time.monotonic() - tick_started)
        return results

    try:
        issues = _scan_queue(config)
    except Exception as exc:  # noqa: BLE001 - run-level failure must surface
        message = _sanitize_error_text(str(exc), token=config.github_token)
        print(f"alchemist: scan failed: {message}", file=sys.stderr)
        results = [_run_level_error(config, f"scan: {message}")]
        _log_tick_summary(results, time.monotonic() - tick_started)
        return results

    if config.repo_blocklist:
        skipped = [i for i in issues if i.repository in config.repo_blocklist]
        for issue in skipped:
            print(
                f"alchemist: skipping {issue.repository}#{issue.number} (repo in blocklist)",
                file=sys.stderr,
            )
        issues = [i for i in issues if i.repository not in config.repo_blocklist]

    work = _select_work(issues, config, limit=config.max_issues_per_tick)

    results: list[RunResult] = []
    if work:
        workers = max(1, min(config.max_concurrent_repos, len(work)))
        if workers == 1:
            for repo, slice_ in work:
                results.extend(_process_repo(repo, slice_, config))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_process_repo, repo, slice_, config) for repo, slice_ in work
                ]
                results.extend([r for fut in futures for r in fut.result()])

    _log_tick_summary(results, time.monotonic() - tick_started)
    return results


def _log_tick_summary(results: list[RunResult], elapsed_sec: float) -> None:
    dispatched = sum(1 for r in results if r.status == "dispatched")
    waiting = sum(1 for r in results if r.status == "waiting")
    open_prs = sum(1 for r in results if r.status in {"pr-open", "nudged", "merge-queued"})
    merged = sum(1 for r in results if r.merged is True)
    blocked = sum(1 for r in results if r.status == "blocked")
    errored = sum(1 for r in results if r.error and r.issue_number != 0)
    fatal = sum(1 for r in results if r.error and r.issue_number == 0)
    print(
        f"alchemist tick: {len(results)} processed "
        f"(dispatched={dispatched} waiting={waiting} pr_open={open_prs} "
        f"merged={merged} blocked={blocked} errored={errored} fatal={fatal}) "
        f"in {elapsed_sec:.1f}s",
        file=sys.stderr,
    )


def _scan_queue(config: Config) -> list[DispatchIssue]:
    labels = (
        config.intake_label,
        _dispatched_label(config.state_label_prefix),
        _pr_open_label(config.state_label_prefix),
    )
    by_key: dict[tuple[str, int], DispatchIssue] = {}
    for label in labels:
        for issue in scan(org=config.org, label=label):
            if _should_ignore_issue(issue, config):
                continue
            by_key[(issue.repository, issue.number)] = issue
    return list(by_key.values())


def _should_ignore_issue(issue: DispatchIssue, config: Config) -> bool:
    labels = {label.lower() for label in issue.labels}
    ignored = {
        _blocked_label(config.state_label_prefix).lower(),
        _shipped_label(config.state_label_prefix).lower(),
        _error_label(config.state_label_prefix).lower(),
        _SKIP_LABEL,
    }
    return not labels.isdisjoint(ignored)


def _process_repo(repo: str, issues: list[DispatchIssue], config: Config) -> list[RunResult]:
    if not issues:
        return []

    if config.dry_run:
        print(f"[DRY-RUN] would ensure labels on {repo}", file=sys.stderr)
    else:
        try:
            _ensure_labels(repo, config)
        except _GhError as exc:
            return [
                _result(
                    repo,
                    issue.number,
                    time.monotonic(),
                    config,
                    error=(
                        f"label-ensure: {_sanitize_error_text(str(exc), token=config.github_token)}"
                    ),
                    status="error",
                )
                for issue in issues
            ]

    try:
        with acquire(
            config.state_dir,
            repo,
            holder_note=f"{len(issues)} issue(s); first=#{issues[0].number}",
            stale_after_sec=config.agent_stale_after_hours * 60 * 60,
        ):
            return [_process_issue(issue, config) for issue in issues]
    except LockBusyError as exc:
        return [
            _result(
                repo,
                issue.number,
                time.monotonic(),
                config,
                error=f"lock-busy: {exc}",
                status="lock-busy",
            )
            for issue in issues
        ]


def _select_work(
    issues: list[DispatchIssue], config: Config, *, limit: int
) -> list[tuple[str, list[DispatchIssue]]]:
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


def _process_issue(issue: DispatchIssue, config: Config) -> RunResult:
    started = time.monotonic()
    try:
        if _has_label(issue, config.intake_label):
            return _dispatch_issue(issue, config, started)
        return _babysit_issue(issue, config, started)
    except Exception as exc:  # noqa: BLE001 - per-issue failures are recoverable
        message = _sanitize_error_text(str(exc), token=config.github_token)
        if not config.dry_run:
            with _SuppressGhErrors():
                body = (
                    "⚠️ alchemist hit an error while coordinating this issue.\n\n"
                    "```text\n"
                    f"{message}\n"
                    "```"
                )
                _post_issue_comment(issue.repository, issue.number, body, config)
                _set_state_label(
                    issue.repository,
                    issue.number,
                    config.state_label_prefix,
                    _error_label(config.state_label_prefix),
                )
        return _result(
            issue.repository,
            issue.number,
            started,
            config,
            error=message,
            status="error",
        )


def _dispatch_issue(issue: DispatchIssue, config: Config, started: float) -> RunResult:
    repo = issue.repository
    if config.dry_run:
        print(
            f"[DRY-RUN] {repo}#{issue.number}: would dispatch to {config.agent_provider}",
            file=sys.stderr,
        )
        return _result(repo, issue.number, started, config, status="dispatched")

    _set_state_label(
        repo,
        issue.number,
        config.state_label_prefix,
        _dispatched_label(config.state_label_prefix),
        remove_extra=(config.intake_label,),
    )
    if _should_set_assignee(config):
        with _SuppressGhErrors():
            _set_assignee(repo, issue.number, "add", config.assignee_user)

    dispatch = _start_agent(issue, config)
    _post_issue_comment(repo, issue.number, _dispatch_comment(issue, config, dispatch), config)
    return _result(
        repo,
        issue.number,
        started,
        config,
        status="dispatched",
    )


def _babysit_issue(issue: DispatchIssue, config: Config, started: float) -> RunResult:
    repo = issue.repository
    pr = _find_issue_pr(issue)
    if pr is None:
        if _issue_is_stale(issue, config):
            message = (
                f"No linked PR was found after {config.agent_stale_after_hours}h. "
                "Marking this dispatch blocked for human triage."
            )
            if not config.dry_run:
                _post_issue_comment(repo, issue.number, f"⏸ alchemist blocked: {message}", config)
                _set_state_label(
                    repo,
                    issue.number,
                    config.state_label_prefix,
                    _blocked_label(config.state_label_prefix),
                )
            return _result(repo, issue.number, started, config, error=message, status="blocked")
        return _result(repo, issue.number, started, config, status="waiting")

    if pr["mergedAt"]:
        if not config.dry_run:
            _post_issue_comment(
                repo, issue.number, f"✅ alchemist saw the PR merge — {pr['url']}", config
            )
            _set_state_label(
                repo,
                issue.number,
                config.state_label_prefix,
                _shipped_label(config.state_label_prefix),
            )
        return _result(
            repo, issue.number, started, config, pr_url=pr["url"], merged=True, status="merged"
        )

    if pr["state"].upper() == "CLOSED":
        message = f"linked PR closed without merge: {pr['url']}"
        if not config.dry_run:
            _post_issue_comment(repo, issue.number, f"⏸ alchemist blocked: {message}", config)
            _set_state_label(
                repo,
                issue.number,
                config.state_label_prefix,
                _blocked_label(config.state_label_prefix),
            )
        return _result(
            repo,
            issue.number,
            started,
            config,
            pr_url=pr["url"],
            merged=False,
            error=message,
            status="blocked",
        )

    if not config.dry_run:
        _set_state_label(
            repo, issue.number, config.state_label_prefix, _pr_open_label(config.state_label_prefix)
        )

    followup = _followup_needed(pr)
    if followup is not None and not _has_nudge(pr, followup):
        if not config.dry_run:
            _nudge_agent(issue, pr, config, followup)
        return _result(
            repo, issue.number, started, config, pr_url=pr["url"], merged=False, status="nudged"
        )

    if config.auto_merge and _pr_ready_to_merge(pr):
        if config.dry_run:
            return _result(
                repo,
                issue.number,
                started,
                config,
                pr_url=pr["url"],
                merged=False,
                status="merge-queued",
            )
        _queue_auto_merge(issue.repository, pr["number"])
        return _result(
            repo,
            issue.number,
            started,
            config,
            pr_url=pr["url"],
            merged=False,
            status="merge-queued",
        )

    return _result(
        repo, issue.number, started, config, pr_url=pr["url"], merged=False, status="pr-open"
    )


def _start_agent(issue: DispatchIssue, config: Config) -> _AgentDispatch:
    provider = config.agent_provider.strip().lower()
    if provider == "codex":
        return _AgentDispatch(provider="codex")
    if provider == "devin":
        return _create_devin_session(issue, config)
    raise _AgentDispatchError(f"unsupported agent_provider: {config.agent_provider}")


def _dispatch_comment(issue: DispatchIssue, config: Config, dispatch: _AgentDispatch) -> str:
    if dispatch.provider == "codex":
        instruction = _agent_prompt(issue, "Codex")
        return (
            f"{_DISPATCH_MARKER} provider=codex -->\n"
            "@codex please take this issue.\n\n"
            f"{instruction}"
        )

    session_line = f"\n\nDevin session: {dispatch.session_url}" if dispatch.session_url else ""
    return (
        f"{_DISPATCH_MARKER} provider=devin session_id={dispatch.session_id or ''} -->\n"
        "alchemist dispatched this issue to Devin via the Devin API."
        f"{session_line}"
    )


def _agent_prompt(issue: DispatchIssue, agent_name: str) -> str:
    return (
        f"Repository: `{issue.repository}`\n"
        f"Issue: {issue.url}\n\n"
        "Please investigate whether the issue is still valid. If it is valid, "
        "make the smallest production-quality fix, add or update tests when appropriate, "
        f"and open a pull request that includes `Closes #{issue.number}` in the body. "
        "If the issue is stale, ambiguous, or not actionable as a code change, explain why "
        "in this thread and do not open a PR.\n\n"
        f"{agent_name} should keep the change scoped to this issue and avoid unrelated cleanup."
    )


def _create_devin_session(issue: DispatchIssue, config: Config) -> _AgentDispatch:
    api_key = os.environ.get(config.devin_api_key_env)
    if not api_key:
        raise _AgentDispatchError(f"${config.devin_api_key_env} not set for Devin dispatch")
    if not config.devin_org_id:
        raise _AgentDispatchError("ALCHEMIST_DEVIN_ORG_ID must be set for Devin dispatch")

    payload = json.dumps({"prompt": _agent_prompt(issue, "Devin")}).encode("utf-8")
    request = urllib.request.Request(
        f"{_DEVIN_API_BASE}/organizations/{config.devin_org_id}/sessions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise _AgentDispatchError(
            f"Devin API HTTP {exc.code}: {_sanitize_error_text(detail, extra_tokens=(api_key,))}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise _AgentDispatchError(f"Devin API session create failed: {exc}") from exc

    session_id = data.get("session_id")
    url = data.get("url")
    if not isinstance(session_id, str) or not session_id:
        raise _AgentDispatchError("Devin API response did not include session_id")
    return _AgentDispatch(
        provider="devin",
        session_id=session_id,
        session_url=url if isinstance(url, str) else None,
    )


def _send_devin_message(session_id: str, message: str, config: Config) -> None:
    api_key = os.environ.get(config.devin_api_key_env)
    if not api_key:
        raise _AgentFollowupError(f"${config.devin_api_key_env} not set for Devin follow-up")
    if not config.devin_org_id:
        raise _AgentFollowupError("ALCHEMIST_DEVIN_ORG_ID must be set for Devin follow-up")

    payload = json.dumps({"message": message}).encode("utf-8")
    request = urllib.request.Request(
        f"{_DEVIN_API_BASE}/organizations/{config.devin_org_id}/sessions/{session_id}/messages",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30):  # noqa: S310
            return
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise _AgentFollowupError(
            f"Devin API HTTP {exc.code}: {_sanitize_error_text(detail, extra_tokens=(api_key,))}"
        ) from exc
    except OSError as exc:
        raise _AgentFollowupError(f"Devin API message failed: {exc}") from exc


def _nudge_agent(issue: DispatchIssue, pr: _PrState, config: Config, reason: str) -> None:
    message = _followup_message(reason, pr["url"])
    marker = f"{_NUDGE_MARKER} {reason} -->"
    if config.agent_provider.strip().lower() == "devin":
        session_id = _find_devin_session_id(issue.repository, issue.number)
        if session_id:
            _send_devin_message(session_id, message, config)
            _post_pr_comment(
                issue.repository,
                pr["number"],
                f"{marker}\nSent follow-up to Devin session `{session_id}`.",
                config,
            )
            return
        _post_pr_comment(issue.repository, pr["number"], f"{marker}\n@devin {message}", config)
        return

    _post_pr_comment(issue.repository, pr["number"], f"{marker}\n@codex {message}", config)


def _followup_needed(pr: _PrState) -> str | None:
    checks = _checks_state(pr)
    if checks == "failure":
        return "checks-failed"
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        return "changes-requested"
    return None


def _followup_message(reason: str, pr_url: str) -> str:
    if reason == "checks-failed":
        return (
            f"The PR has failing checks: {pr_url}. "
            "Please fix only the failing checks and keep scope minimal."
        )
    if reason == "changes-requested":
        return (
            f"The PR has requested changes: {pr_url}. "
            "Please address the review comments without unrelated cleanup."
        )
    return f"Please continue work on {pr_url}."


def _has_nudge(pr: _PrState, reason: str) -> bool:
    marker = f"{_NUDGE_MARKER} {reason} -->"
    for comment in pr.get("comments") or []:
        body = comment.get("body") if isinstance(comment, dict) else None
        if isinstance(body, str) and marker in body:
            return True
    return False


def _pr_ready_to_merge(pr: _PrState) -> bool:
    return (
        pr["state"].upper() == "OPEN"
        and not pr["isDraft"]
        and pr["mergedAt"] is None
        and pr.get("reviewDecision") != "CHANGES_REQUESTED"
        and _checks_state(pr) == "success"
    )


def _checks_state(pr: _PrState) -> str:
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return "pending"
    saw_pending = False
    for check in checks:
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion in {
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
        }:
            return "failure"
        if conclusion in {"SUCCESS", "SKIPPED", "NEUTRAL"}:
            continue
        if status and status not in {"COMPLETED", "SUCCESS"}:
            saw_pending = True
            continue
        saw_pending = True
    return "pending" if saw_pending else "success"


def _find_issue_pr(issue: DispatchIssue) -> _PrState | None:
    candidates = _pr_search(issue.repository, f"#{issue.number}")
    issue_refs = (issue.url, f"#{issue.number}")
    for pr in candidates:
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        if any(ref in text for ref in issue_refs):
            return _pr_state(issue.repository, int(pr["number"]))
    return None


def _pr_search(repo: str, query: str) -> list[dict[str, Any]]:
    result = _run_gh(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--search",
            query,
            "--json",
            "number,title,body,updatedAt",
            "--limit",
            "20",
        ],
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise _GhError(f"gh pr list JSON parse failed: {exc}") from exc
    return payload if isinstance(payload, list) else []


def _pr_state(repo: str, pr_number: int) -> _PrState:
    result = _run_gh(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "url,number,state,mergedAt,reviewDecision,isDraft,statusCheckRollup,comments",
        ],
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise _GhError(f"gh pr view JSON parse failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise _GhError("gh pr view returned non-object JSON")
    return {
        "url": str(payload.get("url") or ""),
        "number": int(payload.get("number") or pr_number),
        "state": str(payload.get("state") or ""),
        "mergedAt": payload.get("mergedAt") if isinstance(payload.get("mergedAt"), str) else None,
        "reviewDecision": (
            payload.get("reviewDecision")
            if isinstance(payload.get("reviewDecision"), str)
            else None
        ),
        "isDraft": bool(payload.get("isDraft")),
        "statusCheckRollup": (
            payload.get("statusCheckRollup")
            if isinstance(payload.get("statusCheckRollup"), list)
            else []
        ),
        "comments": payload.get("comments") if isinstance(payload.get("comments"), list) else [],
    }


def _queue_auto_merge(repo: str, pr_number: int) -> None:
    _run_gh(
        [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            repo,
            "--squash",
            "--auto",
            "--delete-branch",
        ],
        timeout=30,
    )


def _find_devin_session_id(repo: str, issue_number: int) -> str | None:
    result = _run_gh(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "comments",
        ],
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if not isinstance(comments, list):
        return None
    for comment in reversed(comments):
        body = comment.get("body") if isinstance(comment, dict) else None
        if not isinstance(body, str) or _DISPATCH_MARKER not in body:
            continue
        match = re.search(r"\bsession_id=([A-Za-z0-9_.:-]+)", body)
        if match:
            return match.group(1)
    return None


def _issue_is_stale(issue: DispatchIssue, config: Config) -> bool:
    try:
        updated = datetime.fromisoformat(issue.updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(UTC) - updated >= timedelta(hours=config.agent_stale_after_hours)


def _has_label(issue: DispatchIssue, label: str) -> bool:
    needle = label.lower()
    return any(existing.lower() == needle for existing in issue.labels)


def _label_prefix(raw: str) -> str:
    return raw.removesuffix("-dispatch")


def _dispatched_label(prefix: str) -> str:
    return f"{_label_prefix(prefix)}-dispatched"


def _pr_open_label(prefix: str) -> str:
    return f"{_label_prefix(prefix)}-pr-open"


def _blocked_label(prefix: str) -> str:
    return f"{_label_prefix(prefix)}-blocked"


def _shipped_label(prefix: str) -> str:
    return f"{_label_prefix(prefix)}-shipped"


def _error_label(prefix: str) -> str:
    return f"{_label_prefix(prefix)}-error"


def _state_labels(prefix: str) -> tuple[str, ...]:
    return (
        _dispatched_label(prefix),
        _pr_open_label(prefix),
        _blocked_label(prefix),
        _shipped_label(prefix),
        _error_label(prefix),
    )


_LABEL_PALETTE: tuple[tuple[str, str, str], ...] = (
    ("dispatched", "fff5d7", "Alchemist dispatched issue to external agent"),
    ("pr-open", "d7f0ff", "Alchemist found an agent PR and is watching it"),
    ("blocked", "cfd7ff", "Alchemist blocked for human triage"),
    ("shipped", "d7ffd7", "Alchemist saw the PR merge"),
    ("error", "ffd7d7", "Alchemist coordinator error"),
)


def _ensure_labels(repo: str, config: Config) -> None:
    cache_key = (repo, config.intake_label, config.state_label_prefix)
    if cache_key in _LABELS_ENSURED:
        return

    labels = {
        config.intake_label: ("ffd787", "Eligible for Alchemist agent dispatch"),
        _dispatched_label(config.state_label_prefix): _LABEL_PALETTE[0][1:],
        _pr_open_label(config.state_label_prefix): _LABEL_PALETTE[1][1:],
        _blocked_label(config.state_label_prefix): _LABEL_PALETTE[2][1:],
        _shipped_label(config.state_label_prefix): _LABEL_PALETTE[3][1:],
        _error_label(config.state_label_prefix): _LABEL_PALETTE[4][1:],
    }
    for name, (color, description) in labels.items():
        _run_gh(
            [
                "gh",
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            timeout=10,
        )
    _LABELS_ENSURED.add(cache_key)


def _set_state_label(
    repo: str,
    issue_number: int,
    prefix: str,
    add: str,
    *,
    remove_extra: tuple[str, ...] = (),
) -> None:
    current = _current_state_labels(repo, issue_number, prefix)
    current.add(add)
    to_remove = sorted({label for label in current if label != add} | set(remove_extra))
    cmd = ["gh", "issue", "edit", str(issue_number), "--repo", repo]
    if to_remove:
        cmd.extend(["--remove-label", ",".join(to_remove)])
    cmd.extend(["--add-label", add])
    _run_gh(cmd, timeout=30)


def _current_state_labels(repo: str, issue_number: int, prefix: str) -> set[str]:
    result = _run_gh(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "labels",
            "--jq",
            ".labels[].name",
        ],
        timeout=30,
    )
    expected = set(_state_labels(prefix))
    return {line.strip() for line in result.stdout.splitlines() if line.strip() in expected}


def _post_issue_comment(repo: str, issue_number: int, body: str, config: Config) -> None:
    _ = config
    _run_gh(
        ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", body],
        timeout=_COMMENT_TIMEOUT_SEC,
    )


def _post_pr_comment(repo: str, pr_number: int, body: str, config: Config) -> None:
    _ = config
    _run_gh(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
        timeout=_COMMENT_TIMEOUT_SEC,
    )


def _set_assignee(repo: str, issue_number: int, action: str, assignee: str) -> None:
    flag = "--add-assignee" if action == "add" else "--remove-assignee"
    _run_gh(["gh", "issue", "edit", str(issue_number), "--repo", repo, flag, assignee], timeout=30)


def _should_set_assignee(config: Config) -> bool:
    return bool(
        config.assignee_user and config.assignee_user != "@me" and not config.has_app_credentials
    )


def _run_gh(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _GhError("gh not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise _GhError(f"gh command timed out after {timeout}s") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"gh exit {result.returncode}"
        raise _GhError(_sanitize_error_text(detail))
    return result


def _sanitize_error_text(
    text: str,
    *,
    token: str | None = None,
    extra_tokens: tuple[str, ...] = (),
) -> str:
    sanitized = text
    if token:
        sanitized = sanitized.replace(token, "[redacted-token]")
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        sanitized = sanitized.replace(encoded, "[redacted-git-basic-auth]")
    for secret in extra_tokens:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted-secret]")
    devin_key = os.environ.get("DEVIN_API_KEY")
    if devin_key:
        sanitized = sanitized.replace(devin_key, "[redacted-devin-key]")
    sanitized = _AUTH_HEADER_RE.sub("Authorization: Basic [redacted]", sanitized)
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9_.:-]+", "Bearer [redacted]", sanitized)
    sanitized = _DEVIN_TOKEN_RE.sub("[redacted-devin-key]", sanitized)
    return _GITHUB_TOKEN_RE.sub("[redacted-token]", sanitized)


def _run_level_error(config: Config, error: str) -> RunResult:
    return RunResult(
        repo=config.org,
        issue_number=0,
        pr_url=None,
        merged=None,
        error=error,
        elapsed_sec=0.0,
        dry_run=config.dry_run,
        status="fatal",
    )


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
    status: str,
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
        status=status,
    )


class _SuppressGhErrors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc is None:
            return False
        if isinstance(exc, _GhError):
            print(f"alchemist: warning: {_sanitize_error_text(str(exc))}", file=sys.stderr)
            return True
        return False


__all__ = [
    "RunResult",
    "run_tick",
]
