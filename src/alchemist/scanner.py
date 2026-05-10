"""Issue scanner — finds labelled issues across all repos in an org.

Single GitHub search query:
`gh search issues --owner <org> --label <label> --state open --archived=false`.
Returns matching issues across every active repo in the org. Auto-picks-up new
repos, auto-drops archived ones. The org is the unit of scoping.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class DispatchIssue:
    """A GitHub issue tagged for alchemist dispatch."""

    number: int
    title: str
    body: str
    url: str
    repository: str        # "owner/name"
    updated_at: str
    labels: tuple[str, ...]


class ScanError(RuntimeError):
    """gh CLI invocation failed."""


def scan(
    *,
    org: str,
    label: str,
    limit: int = 1000,
    gh_bin: str = "gh",
    extra_args: tuple[str, ...] = (),
) -> list[DispatchIssue]:
    """Run a `gh search issues` query and return matching issues.

    Raises ScanError on non-zero gh exit. Returns an empty list when there
    are no matches (gh emits `[]`).
    """
    if limit < 1:
        raise ScanError("scan limit must be at least 1")
    cmd: list[str] = [
        gh_bin, "search", "issues",
        "--owner", org,
        "--label", label,
        "--state", "open",
        "--archived=false",
        "--limit", str(limit),
        "--json", "number,title,body,url,repository,updatedAt,labels",
        *extra_args,
    ]
    try:
        result = subprocess.run(  # noqa: S603 — gh args are constructed from validated config
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ScanError(f"`{gh_bin}` not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScanError("gh search issues timed out after 60s") from exc

    if result.returncode != 0:
        raise ScanError(
            f"gh search issues exited {result.returncode}: {result.stderr.strip()}"
        )

    return _parse_issues(result.stdout)


def _parse_issues(raw_json: str) -> list[DispatchIssue]:
    if not raw_json.strip():
        return []
    try:
        items = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ScanError(f"gh emitted invalid JSON: {exc}") from exc

    issues: list[DispatchIssue] = []
    for item in items:
        repo = item.get("repository") or {}
        repo_full = repo.get("nameWithOwner") or repo.get("name") or ""
        labels = tuple(label.get("name", "") for label in item.get("labels") or [])
        issues.append(
            DispatchIssue(
                number=int(item["number"]),
                title=str(item.get("title") or ""),
                body=str(item.get("body") or ""),
                url=str(item.get("url") or ""),
                repository=str(repo_full),
                updated_at=str(item.get("updatedAt") or ""),
                labels=labels,
            )
        )
    return issues
