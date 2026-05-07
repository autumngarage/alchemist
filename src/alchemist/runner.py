"""The transmute loop — one tick of `alchemist run-once`.

End-to-end:
    scan → for each issue (capped at max_per_tick):
        lock → label working → clone → brief → conductor → review →
        push → PR → label shipped → unlock

Composition is by subprocess, never by code import (Doctrine 0001/0003/0004).
Every external call has an explicit `timeout=` and propagates structured
errors via `RunResult.error`. No retries inside alchemist — the dispatch
label *is* the retry contract.

Dry-run rules: when `config.dry_run=True`, every read-side operation runs
(scan, lock, clone, brief, conductor, review) but every mutation is
skipped (label transitions, git push, gh pr create). The intent is to
prove the pipeline against test issues without touching real PR state.
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


@dataclass(frozen=True)
class RunResult:
    repo: str
    issue_number: int
    pr_url: str | None
    review_verdict: str | None  # "CLEAN" | "FIXED" | "BLOCKED" | None on dry-run/error
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

    note = f"{len(issues)} issue(s); first=#{issues[0].number}"
    try:
        with acquire(config.state_dir, repo, holder_note=note):
            return [_process_issue(issue, config) for issue in issues]
    except LockBusyError as exc:
        # Another worker (different tick, or different thread) holds the
        # repo. Skip — next tick will pick these up.
        return [
            RunResult(
                repo=repo,
                issue_number=i.number,
                pr_url=None,
                review_verdict=None,
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
            review_verdict=None,
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
        default_branch = _default_branch(repo, config)
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

    brief_path = config.state_dir / "briefs" / f"{repo.replace('/', '-')}-{issue.number}.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(render_brief(issue, repo))

    transcript_path = (
        config.state_dir / "transcripts" / f"{repo.replace('/', '-')}-{issue.number}.log"
    )
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        cost_summary = _run_conductor(
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

    if not config.dry_run:
        try:
            _stage_and_commit(work_dir, f"alchemist: {issue.title}")
        except subprocess.SubprocessError as exc:
            return _bail(repo, issue, started, config, f"commit: {exc}")

    try:
        verdict, summary = _run_review(work_dir, default_branch, config.review_timeout_sec)
    except _ToolError as exc:
        return _bail(repo, issue, started, config, f"review: {exc}")

    if config.dry_run:
        msg = (
            f"[DRY-RUN] {repo}#{issue.number}: review={verdict}; "
            f"would push branch {branch} and open PR"
        )
        print(msg, file=sys.stderr)
        return _result(
            repo, issue.number, started, config,
            review_verdict=verdict,
        )

    try:
        _push_branch(work_dir, branch, repo, token)
    except subprocess.SubprocessError as exc:
        return _bail(repo, issue, started, config, f"push: {exc}")

    body = render_pr_body(
        issue=issue,
        review_verdict=verdict,
        review_summary=summary,
        cost_summary=cost_summary,
        provider=config.default_provider,
        dry_run=False,
    )
    pr_title = f"fix: {issue.title} (#{issue.number})"
    try:
        pr_url = _make_pr(repo, default_branch, branch, pr_title, body, token)
    except _GhError as exc:
        return _bail(repo, issue, started, config, f"pr-create: {exc}")

    try:
        _set_label(repo, issue.number, _shipped_label(config.dispatch_label), config)
    except _GhError as exc:
        # PR is already open; tag the failure to transition labels but keep success
        print(f"alchemist: warning — label transition to shipped failed: {exc}", file=sys.stderr)

    return _result(
        repo, issue.number, started, config,
        pr_url=pr_url,
        review_verdict=verdict,
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


def _default_branch(repo: str, config: Config) -> str:  # noqa: ARG001 — config retained for future use
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
) -> str | None:
    """Run conductor exec; return the cost-summary tail or None.

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

    try:
        tail = transcript_path.read_text().splitlines()
    except OSError:
        return None
    cost_lines = [line for line in tail if "cost" in line.lower() or "$" in line]
    return "\n".join(cost_lines[-5:]) if cost_lines else None


def _has_changes(repo_dir: Path) -> bool:
    result = subprocess.run(  # noqa: S603,S607
        ["git", "status", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True, timeout=10,
    )
    return bool(result.stdout.strip())


def _stage_and_commit(repo_dir: Path, message: str) -> None:
    subprocess.run(  # noqa: S603,S607
        ["git", "add", "-A"],
        cwd=repo_dir, check=True, timeout=30,
    )
    subprocess.run(  # noqa: S603,S607
        ["git", "commit", "-m", message],
        cwd=repo_dir, check=True, timeout=30,
    )


def _resolve_touchstone_root() -> Path:
    """Locate the touchstone install (the repo, not the bin shim)."""
    env_root = os.environ.get("TOUCHSTONE_ROOT")
    if env_root:
        candidate = Path(env_root)
        if (candidate / "scripts" / "codex-review.sh").exists():
            return candidate
        if (candidate / "libexec" / "scripts" / "codex-review.sh").exists():
            return candidate / "libexec"

    try:
        result = subprocess.run(  # noqa: S603,S607
            ["brew", "--prefix", "touchstone"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            brew_root = Path(result.stdout.strip())
            if (brew_root / "libexec" / "scripts" / "codex-review.sh").exists():
                return brew_root / "libexec"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    for fallback in (Path("/opt/touchstone"), Path("/opt/touchstone/libexec")):
        if (fallback / "scripts" / "codex-review.sh").exists():
            return fallback
    raise _ToolError("touchstone codex-review.sh not found")


def _run_review(
    repo_dir: Path, base_branch: str, timeout: int
) -> tuple[str, str | None]:
    """Run touchstone codex-review.sh; return (verdict, summary).

    Verdicts: 'CLEAN' (zero exit), 'FIXED' (script auto-committed), 'BLOCKED'.
    Touchstone exits 0 on CLEAN and FIXED, non-zero on BLOCKED.
    """
    root = _resolve_touchstone_root()
    script = root / "scripts" / "codex-review.sh"
    env = {**os.environ, "TOUCHSTONE_REVIEW_BASE_REF": base_branch}
    try:
        result = subprocess.run(  # noqa: S603
            ["bash", str(script)],
            cwd=repo_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ToolError(f"review timeout after {timeout}s") from exc

    output = (result.stdout or "") + (result.stderr or "")
    if "CODEX_REVIEW_BLOCKED" in output or result.returncode != 0:
        return "BLOCKED", _trim_review_output(output)
    if "CODEX_REVIEW_FIXED" in output:
        return "FIXED", _trim_review_output(output)
    return "CLEAN", _trim_review_output(output)


def _trim_review_output(text: str, max_lines: int = 60) -> str | None:
    lines = text.splitlines()
    if not lines:
        return None
    return "\n".join(lines[-max_lines:])


def _push_branch(repo_dir: Path, branch: str, repo: str, token: str) -> None:
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    subprocess.run(  # noqa: S603,S607
        ["git", "push", "--set-upstream", url, branch],
        cwd=repo_dir, check=True, timeout=120,
    )


def _make_pr(
    repo: str, base: str, head: str, title: str, body: str, token: str  # noqa: ARG001 — token reserved for future direct API use
) -> str:
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
    return url


def _post_error_comment(repo: str, issue_number: int, message: str) -> None:
    cmd = [
        "gh", "issue", "comment", str(issue_number),
        "--repo", repo,
        "--body", f"alchemist: {message}",
    ]
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603


def _bail(
    repo: str, issue: DispatchIssue, started: float, config: Config, message: str
) -> RunResult:
    """Common error path: post a comment, transition to error label, return result."""
    if not config.dry_run:
        _post_error_comment(repo, issue.number, message)
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
    review_verdict: str | None = None,
    error: str | None = None,
) -> RunResult:
    return RunResult(
        repo=repo,
        issue_number=issue_number,
        pr_url=pr_url,
        review_verdict=review_verdict,
        error=error,
        elapsed_sec=time.monotonic() - started,
        dry_run=config.dry_run,
    )


__all__ = [
    "BRIEF_TEMPLATE_VERSION",
    "RunResult",
    "run_tick",
]
