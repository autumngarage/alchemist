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

import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from alchemist.briefs import BRIEF_TEMPLATE_VERSION, render_brief, render_pr_body
from alchemist.doctor import run_doctor
from alchemist.locks import LockBusyError, acquire
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


def run_tick(config: Config) -> list[RunResult]:
    """Process one tick worth of dispatched issues.

    Issues are grouped by repo. Within a repo, only one worker runs at a
    time (the per-repo lock is the constraint). Across repos, up to
    `max_concurrent_repos` workers run in parallel — that's the swarm.
    """
    checks = run_doctor(config)
    failed = [c for c in checks if not c.ok]
    if failed:
        names = ", ".join(c.name for c in failed)
        print(
            f"alchemist: doctor failed ({names}); skipping tick",
            file=sys.stderr,
        )
        return []

    try:
        issues = scan(org=config.org, label=config.dispatch_label)
    except Exception as exc:  # noqa: BLE001 — surface any scanner failure to operator
        print(f"alchemist: scan failed: {exc}", file=sys.stderr)
        return []

    grouped: dict[str, list[DispatchIssue]] = defaultdict(list)
    for issue in issues:
        grouped[issue.repository].append(issue)

    work: list[tuple[str, list[DispatchIssue]]] = [
        (repo, sorted(group, key=lambda i: i.updated_at)[: config.max_per_repo_per_tick])
        for repo, group in grouped.items()
    ]

    if not work:
        return []

    workers = max(1, min(config.max_concurrent_repos, len(work)))
    if workers == 1:
        results: list[RunResult] = []
        for repo, slice_ in work:
            results.extend(_process_repo(repo, slice_, config))
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_repo, repo, slice_, config) for repo, slice_ in work]
        return [r for fut in futures for r in fut.result()]


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
        with acquire(config.state_dir, repo, holder_note=note):
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


def _process_issue(issue: DispatchIssue, config: Config) -> RunResult:
    started = time.monotonic()
    try:
        return _process_locked(issue, config, started)
    except Exception as exc:  # noqa: BLE001 — every per-issue failure is recoverable
        return RunResult(
            repo=issue.repository,
            issue_number=issue.number,
            pr_url=None,
            merged=None,
            error=f"unhandled: {exc}",
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

    # Claim the issue (alchemist#23): assign + post a "starting work" comment
    # so the audit trail is visible, not just the -working label transition.
    # Assignee failure is non-fatal: log + continue. The comment is the backup.
    if not config.dry_run:
        try:
            _set_assignee(repo, issue.number, "add", config.assignee_user, config)
        except _GhError as exc:
            print(
                f"alchemist: warning — could not assign {config.assignee_user}: {exc}",
                file=sys.stderr,
            )
        _post_activity_comment(
            repo, issue.number,
            f"alchemist: claiming this issue\n"
            f"- branch: `{branch}`\n"
            f"- provider: `{config.default_provider}`",
            config,
        )

    brief_path = config.state_dir / "briefs" / f"{repo.replace('/', '-')}-{issue.number}.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(render_brief(issue, repo, work_dir))

    transcript_path = (
        config.state_dir / "transcripts" / f"{repo.replace('/', '-')}-{issue.number}.log"
    )
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _run_conductor(
            brief_path=brief_path,
            cwd=work_dir,
            provider=config.default_provider,
            timeout=config.conductor_timeout_sec,
            transcript_path=transcript_path,
        )
    except _ToolError as exc:
        return _bail(repo, issue, started, config, f"conductor: {exc}")

    if not _has_changes(work_dir):
        return _bail(
            repo,
            issue,
            started,
            config,
            "conductor produced no diff",
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
        return _bail(repo, issue, started, config, f"push: {exc}")

    body = render_pr_body(
        issue=issue,
        provider=config.default_provider,
    )
    pr_title = f"{_PR_TITLE_PREFIX} fix: {issue.title} (#{issue.number})"
    try:
        pr_url, pr_number = _make_pr(repo, default_branch, branch, pr_title, body)
    except _GhError as exc:
        return _bail(repo, issue, started, config, f"pr-create: {exc}")

    # Touchstone owns the review-and-merge gate. Alchemist hands off the PR
    # number and waits for the result. CLEAN review → squash-merged. BLOCKED
    # review → PR stays open with review comments; needs human triage.
    try:
        merged = _run_merge_pr(work_dir, pr_number, config.review_timeout_sec)
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

    if merged:
        _post_activity_comment(
            repo, issue.number,
            f"alchemist: shipped — see {pr_url}",
            config,
        )
        with contextlib.suppress(_GhError):
            _set_label(repo, issue.number, _shipped_label(config.dispatch_label), config)

    return _result(
        repo, issue.number, started, config,
        pr_url=pr_url, merged=merged,
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


class _GhError(RuntimeError):
    pass


class _ToolError(RuntimeError):
    pass


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _branch_name(issue: DispatchIssue) -> str:
    slug = _SLUG_RE.sub("-", issue.title.lower()).strip("-")[:40]
    return f"alchemist/issue-{issue.number}-{slug}" if slug else f"alchemist/issue-{issue.number}"


def _working_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for the dispatch → working transition."""
    add = dispatch.replace("-dispatch", "-working").replace("-test", "-test-working")
    return dispatch, add


def _shipped_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for working → shipped."""
    working = _working_label(dispatch)[1]
    add = dispatch.replace("-dispatch", "-shipped").replace("-test", "-test-shipped")
    return working, add


def _error_label(dispatch: str) -> tuple[str, str]:
    """Return (remove, add) for any → error."""
    working = _working_label(dispatch)[1]
    add = dispatch.replace("-dispatch", "-error").replace("-test", "-test-error")
    return working, add


_LABEL_PALETTE: tuple[tuple[str, str, str], ...] = (
    # (suffix-key, color, description). The first row is the bare dispatch
    # label; the next three are the state-machine successors derived from it.
    ("base",    "ffd787", "Dispatched to Alchemist for transmutation"),
    ("working", "fff5d7", "Alchemist actively working"),
    ("shipped", "d7ffd7", "Alchemist shipped a PR"),
    ("error",   "ffd7d7", "Alchemist hit an error"),
)


def _expected_labels(dispatch_label: str) -> dict[str, tuple[str, str]]:
    """Return {label_name: (color, description)} for the four labels alchemist
    needs on every watched repo."""
    base = dispatch_label
    working = _working_label(dispatch_label)[1]
    shipped = _shipped_label(dispatch_label)[1]
    error = _error_label(dispatch_label)[1]
    names = {"base": base, "working": working, "shipped": shipped, "error": error}
    return {
        names[key]: (color, desc)
        for key, color, desc in _LABEL_PALETTE
    }


def _ensure_labels(repo: str, dispatch_label: str) -> None:
    """Idempotently create alchemist's expected label set on `repo`.

    Removes the manual-setup cliff for new operators (alchemist#19): the four
    labels alchemist transitions between (`<base>`, `-working`, `-shipped`,
    `-error`) must exist on the target repo or `gh issue edit --add-label`
    silently fails and the dispatch label gets stripped without a successor.

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


def _set_label(repo: str, issue_number: int, transition: tuple[str, str], config: Config) -> None:
    if config.dry_run:
        return
    remove, add = transition
    cmd = [
        "gh", "issue", "edit", str(issue_number),
        "--repo", repo,
        "--remove-label", remove,
        "--add-label", add,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    if result.returncode != 0:
        raise _GhError(result.stderr.strip() or f"gh issue edit exit {result.returncode}")


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


def _clone_or_update(repo: str, dest: Path, default_branch: str, token: str) -> None:
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    if dest.exists() and (dest / ".git").exists():
        subprocess.run(  # noqa: S603,S607
            ["git", "remote", "set-url", "origin", url],
            cwd=dest, check=True, timeout=30,
        )
        subprocess.run(  # noqa: S603,S607
            ["git", "fetch", "origin", default_branch, "--depth", "50"],
            cwd=dest, check=True, timeout=120,
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
    subprocess.run(  # noqa: S603,S607
        ["git", "clone", "--depth", "50", "--branch", default_branch, url, str(dest)],
        check=True, timeout=300,
    )


def _make_branch(dest: Path, branch: str, base: str) -> None:
    subprocess.run(  # noqa: S603,S607
        ["git", "checkout", "-B", branch, base],
        cwd=dest, check=True, timeout=30,
    )


def _run_conductor(
    *,
    brief_path: Path,
    cwd: Path,
    provider: str,
    timeout: int,
    transcript_path: Path,
) -> None:
    """Run conductor exec; on success, conductor's edits are present in cwd.

    Stdout is streamed to the transcript file so an operator can `cat` it
    after the fact. Conductor's own --timeout flag is set in addition to
    subprocess timeout for belt-and-suspenders.
    """
    cmd = [
        "conductor", "exec",
        "--with", provider,
        "--tools", "Read,Edit,Write,Bash",
        "--brief-file", str(brief_path),
        "--cwd", str(cwd),
        "--timeout", str(timeout),
    ]
    with transcript_path.open("w") as fh:
        try:
            result = subprocess.run(  # noqa: S603,S607
                cmd,
                stdout=fh, stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _ToolError(f"timeout after {timeout + 30}s") from exc

    if result.returncode != 0:
        raise _ToolError(f"exit {result.returncode}; see {transcript_path}")


def _has_changes(repo_dir: Path) -> bool:
    result = subprocess.run(  # noqa: S603,S607
        ["git", "status", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True, timeout=10,
    )
    return bool(result.stdout.strip())


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


def _run_merge_pr(repo_dir: Path, pr_number: int, timeout: int) -> bool:
    """Hand the PR to touchstone's merge-pr.sh (review + auto-merge gate).

    Returns:
        True  — touchstone reviewed CLEAN/FIXED and squash-merged the PR.
        False — touchstone review BLOCKED or merge otherwise failed; PR stays
                open with review comments for human triage.

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

    return result.returncode == 0


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


def _push_branch(repo_dir: Path, branch: str, repo: str, token: str) -> None:
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    subprocess.run(  # noqa: S603,S607
        ["git", "push", "--set-upstream", url, branch],
        cwd=repo_dir, check=True, timeout=120,
    )


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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603
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
    repo: str, issue_number: int, message: str, config: Config,
) -> None:
    """Backwards-compat shim around _post_activity_comment for the bail path."""
    _post_activity_comment(repo, issue_number, f"alchemist: {message}", config)


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


def _bail(
    repo: str, issue: DispatchIssue, started: float, config: Config, message: str
) -> RunResult:
    """Common error path: post a comment, transition to error label, return result."""
    if not config.dry_run:
        _post_error_comment(repo, issue.number, message, config)
        with contextlib.suppress(_GhError):
            _set_label(repo, issue.number, _error_label(config.dispatch_label), config)
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
) -> RunResult:
    return RunResult(
        repo=repo,
        issue_number=issue_number,
        pr_url=pr_url,
        merged=merged,
        error=error,
        elapsed_sec=time.monotonic() - started,
        dry_run=config.dry_run,
    )


__all__ = [
    "BRIEF_TEMPLATE_VERSION",
    "RunResult",
    "run_tick",
]
