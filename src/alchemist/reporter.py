"""Internal reporter for alchemist tool failures."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alchemist.config import Config


def report_tool_failure(
    config: Config,
    tool_name: str,
    error_message: str,
    repo_context: str | None = None,
    issue_number: int | None = None,
) -> None:
    """File a GitHub issue in the tool's repository when it fails.

    This ensures that if Conductor or Touchstone break during a remote run,
    the failure is tracked in their own repo for the developer to see.
    """
    if config.dry_run:
        return

    # Map tool names to their respective repositories
    tool_repos = {
        "conductor": "autumngarage/conductor",
        "touchstone": "autumngarage/touchstone",
        "alchemist": "autumngarage/alchemist",
    }

    target_repo = tool_repos.get(tool_name.lower())
    if not target_repo:
        return

    title = f"Tool Failure: {tool_name} error in {config.org}"
    body = (
        f"Alchemist encountered a failure in **{tool_name}** while processing a tick.\n\n"
        f"**Error:** `{error_message}`\n"
    )

    if repo_context:
        body += f"**Context Repo:** {repo_context}\n"
    if issue_number:
        body += f"**Context Issue:** #{issue_number}\n"

    body += "\n---\n*Reported automatically by Alchemist.*"

    # Use 'gh' to file the issue. We don't want to spam, so we check if an
    # open issue with the same title already exists.
    try:
        # Check for existing open issues with same title
        check_cmd = [
            "gh", "issue", "list",
            "--repo", target_repo,
            "--search", f'"{title}" in:title state:open',
            "--json", "number",
            "--jq", "length",
        ]
        result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip() != "0":
            # Already reported and still open
            return

        # Create the issue
        create_cmd = [
            "gh", "issue", "create",
            "--repo", target_repo,
            "--title", title,
            "--body", body,
            "--label", "bug",
        ]
        subprocess.run(create_cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        # If reporting fails, we don't want to crash the main loop
        pass
