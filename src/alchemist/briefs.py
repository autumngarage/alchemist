"""Brief renderer — turns a DispatchIssue into the prompt handed to conductor.

The brief lives in `src/alchemist/templates/brief.md.j2` and ships with the
wheel via `[tool.hatch.build.targets.wheel.force-include]`. Every render
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
    from alchemist.scanner import DispatchIssue


BRIEF_TEMPLATE_VERSION = "1"


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


def render_brief(issue: DispatchIssue, repo: str) -> str:
    """Render the issue → brief markdown."""
    src = _load_template_source("brief.md.j2")
    return _env().from_string(src).render(issue=issue, repo=repo)


def render_pr_body(
    *,
    issue: DispatchIssue,
    review_verdict: str,
    review_summary: str | None,
    cost_summary: str | None,
    provider: str,
    dry_run: bool,
) -> str:
    """Render the PR body markdown for `gh pr create --body <...>`."""
    src = _load_template_source("pr_body.md.j2")
    return _env().from_string(src).render(
        issue=issue,
        review_verdict=review_verdict,
        review_summary=review_summary,
        cost_summary=cost_summary,
        provider=provider,
        brief_version=BRIEF_TEMPLATE_VERSION,
        branch_kind="dry-run" if dry_run else "live",
    )
