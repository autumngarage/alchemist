"""Brief renderer — turns a DispatchIssue into the prompt handed to conductor.

The brief lives in `src/alchemist/templates/brief.md.j2` and ships with the
wheel as package data under `alchemist.templates`. Every render
embeds the issue body inside `<untrusted-input>` delimiters with an explicit
preamble warning the LLM not to follow injected instructions — the issue
body is user-controlled input and must not be treated as a directive.

Bump BRIEF_TEMPLATE_VERSION whenever the template's *contract* changes
(new sections, restructured preamble). Cosmetic edits don't require a bump
but are still version-controlled because the template is checked into source.
"""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

from jinja2 import Environment, StrictUndefined

if TYPE_CHECKING:
    from pathlib import Path

    from alchemist.scanner import DispatchIssue


BRIEF_TEMPLATE_VERSION = "1"

_CONVENTIONS_MAX_CHARS = 8 * 1024
_CONVENTIONS_FILES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")


def _load_template_source(name: str) -> str:
    """Read a template's text from the installed package."""
    pkg = resources.files("alchemist").joinpath("templates").joinpath(name)
    return pkg.read_text(encoding="utf-8")


def _env() -> Environment:
    return Environment(
        autoescape=False,  # output is markdown for an LLM, not HTML
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )


def _load_conventions(repo_dir: Path) -> tuple[str, str] | tuple[None, None]:
    """Load project conventions from CLAUDE.md or AGENTS.md in priority order."""
    for name in _CONVENTIONS_FILES:
        path = repo_dir / name
        if not path.exists():
            continue

        text = path.read_text(encoding="utf-8")
        if len(text) > _CONVENTIONS_MAX_CHARS:
            text = (
                f"{text[:_CONVENTIONS_MAX_CHARS]}\n\n"
                f"[truncated; see full file at {name}]"
            )
        return text, name

    return None, None


def render_brief(issue: DispatchIssue, repo: str, repo_dir: Path) -> str:
    """Render the issue → brief markdown."""
    conventions, conventions_source = _load_conventions(repo_dir)
    src = _load_template_source("brief.md.j2")
    return _env().from_string(src).render(
        issue=issue,
        repo=repo,
        conventions=conventions,
        conventions_source=conventions_source,
    )


def render_pr_body(
    *,
    issue: DispatchIssue,
    provider: str,
    agent_summary: str | None = None,
) -> str:
    """Render the PR body markdown for `gh pr create --body <...>`.

    Includes a link to the source issue, an Alchemist attribution footer, and
    optionally a short "agent summary" section pulled from the conductor
    transcript so reviewers see how the agent interpreted the task without
    opening the full transcript. The review-and-merge gate runs *after* the
    PR opens (Touchstone's `merge-pr.sh`); review findings show up as PR
    comments rather than embedded in the body.
    """
    src = _load_template_source("pr_body.md.j2")
    return _env().from_string(src).render(
        issue=issue,
        provider=provider,
        brief_version=BRIEF_TEMPLATE_VERSION,
        agent_summary=agent_summary,
    )
