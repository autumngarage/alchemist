"""Internal reporter for alchemist/tool failures."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alchemist.config import Config


ALCHEMIST_REPO = "autumngarage/alchemist"


class ReportError(RuntimeError):
    """A best-effort GitHub issue report could not be completed."""


def report_tool_failure(
    config: Config,
    tool_name: str,
    error_message: str,
    repo_context: str | None = None,
    issue_number: int | None = None,
    *,
    auto_dispatch_self: bool = False,
) -> None:
    """File a GitHub issue in the tool's repository when it fails.

    This ensures that if Conductor or Touchstone break during a remote run,
    the failure is tracked in their own repo for the developer to see.
    Alchemist self-reports go through the same issue + label loop as every
    other fix; callers must opt in before the self-issue receives the dispatch
    label.
    """
    if config.dry_run:
        return

    normalized_tool = tool_name.lower()
    title = f"Tool Failure: {tool_name} error in {config.org}"
    body = _tool_failure_body(tool_name, error_message, repo_context, issue_number)

    if normalized_tool == "alchemist":
        report_self_issue(
            config,
            title=title,
            body=body,
            fingerprint=f"tool-failure:{_fingerprint(title, body)}",
            safe_to_dispatch=auto_dispatch_self,
        )
        return

    # Map tool names to their respective repositories
    tool_repos = {
        "conductor": "autumngarage/conductor",
        "touchstone": "autumngarage/touchstone",
    }

    target_repo = tool_repos.get(normalized_tool)
    if not target_repo:
        return

    try:
        _file_issue(
            repo=target_repo,
            title=title,
            body=body,
            labels=("bug",),
            dedupe_query=f'"{title}" in:title state:open',
        )
    except Exception as exc:  # noqa: BLE001 - reporter is best-effort, but never silent
        _warn(f"could not report {tool_name} failure to {target_repo}: {exc}")


def report_self_issue(
    config: Config,
    *,
    title: str,
    body: str,
    fingerprint: str | None = None,
    safe_to_dispatch: bool = False,
) -> None:
    """File a normal Alchemist issue, optionally entering the dispatch queue.

    This is intentionally not a private self-healing path. The lightweight
    recursion contract is:
        observe Alchemist bug -> file Alchemist issue -> maybe label dispatch
        -> next cron tick fixes it through Conductor + Touchstone like any
        other Autumn Garage repo.

    Invariant: at most one self-dispatched issue may be in `*-working`; if one
    is already active, this function still files the issue but does not add the
    dispatch label.
    """
    if config.dry_run:
        return

    marker = fingerprint or f"self-report:{_fingerprint(title, body)}"
    labels = ["bug"]
    policy_note = _self_report_policy_note(config, marker)

    if safe_to_dispatch:
        if _self_work_in_progress(config):
            policy_note += (
                "\n\nSelf-dispatch skipped because another Alchemist issue is already "
                f"labelled `{_working_label(config.dispatch_label)}`."
            )
        else:
            labels.append(config.dispatch_label)
            policy_note += (
                f"\n\nThis issue was labelled `{config.dispatch_label}` because the "
                "caller classified it as a narrow self-fix."
            )
    else:
        policy_note += (
            "\n\nThis issue was filed for human triage and was not auto-dispatched."
        )

    try:
        _file_issue(
            repo=ALCHEMIST_REPO,
            title=title,
            body=f"{body.rstrip()}\n\n{policy_note}",
            labels=tuple(labels),
            dedupe_query=f'"alchemist-fingerprint:{marker}" in:body state:open',
        )
    except Exception as exc:  # noqa: BLE001 - reporter is best-effort, but never silent
        _warn(f"could not report Alchemist self-issue to {ALCHEMIST_REPO}: {exc}")


def _tool_failure_body(
    tool_name: str,
    error_message: str,
    repo_context: str | None,
    issue_number: int | None,
) -> str:
    body = (
        f"Alchemist encountered a failure in **{tool_name}** while processing a tick.\n\n"
        f"**Error:** `{error_message}`\n"
    )
    if repo_context:
        body += f"**Context Repo:** {repo_context}\n"
    if issue_number:
        body += f"**Context Issue:** #{issue_number}\n"

    return body + "\n---\n*Reported automatically by Alchemist.*"


def _self_report_policy_note(config: Config, marker: str) -> str:
    return (
        "## Alchemist self-report policy\n\n"
        "This is a normal GitHub issue. If it receives the dispatch label, the "
        "existing Railway cron will process it through the same branch, Conductor, "
        "PR, and Touchstone merge-gate path used for other Autumn Garage tools.\n\n"
        f"Fingerprint: `alchemist-fingerprint:{marker}`\n"
        f"Dispatch label: `{config.dispatch_label}`"
    )


def _file_issue(
    *,
    repo: str,
    title: str,
    body: str,
    labels: tuple[str, ...],
    dedupe_query: str,
) -> None:
    """Create a GitHub issue unless an open duplicate already exists."""
    check_cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--search", dedupe_query,
        "--json", "number",
        "--jq", "length",
    ]
    result = _run(check_cmd, timeout=15)
    if result.stdout.strip() != "0":
        return

    create_cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
    ]
    for label in labels:
        create_cmd.extend(["--label", label])
    _run(create_cmd, timeout=30)


def _self_work_in_progress(config: Config) -> bool:
    cmd = [
        "gh", "issue", "list",
        "--repo", ALCHEMIST_REPO,
        "--label", _working_label(config.dispatch_label),
        "--state", "open",
        "--json", "number",
        "--jq", "length",
    ]
    try:
        result = _run(cmd, timeout=15)
    except ReportError as exc:
        _warn(f"could not check active Alchemist self-work; skipping self-dispatch: {exc}")
        return True
    return result.stdout.strip() != "0"


def _working_label(dispatch_label: str) -> str:
    return dispatch_label.replace("-dispatch", "-working").replace(
        "-test", "-test-working"
    )


def _fingerprint(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ReportError(str(exc)) from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ReportError(f"{cmd[0]} {' '.join(cmd[1:3])}: {detail}")
    return result


def _warn(message: str) -> None:
    print(f"alchemist: reporter warning: {message}", file=sys.stderr)
