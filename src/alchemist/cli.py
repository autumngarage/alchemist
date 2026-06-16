"""CLI entry point for alchemist."""

from __future__ import annotations

import contextlib
import json
import os
import sys

import click

from alchemist import __version__
from alchemist.auth_token import AuthTokenError, mint_installation_token
from alchemist.banner import (
    SUBTITLE_DOCTOR,
    SUBTITLE_SCAN,
    SUBTITLE_TAGLINE,
    print_banner,
)
from alchemist.config import Config, load_config
from alchemist.doctor import run_doctor
from alchemist.scanner import ScanError, scan


def _resolve_github_token(config: Config) -> str | None:
    """Mint a GitHub App installation token (or pass through the PAT) and
    populate `$GITHUB_TOKEN` in this process's environment.

    Every alchemist command that shells out to `gh` needs `GITHUB_TOKEN` set on
    the spawned subprocess. With App auth, the token is minted fresh per command
    rather than sourced from a static env var. Setting `os.environ` here lets the
    rest of the runner stay auth-mechanism-agnostic.

    Returns the token string (for callers that want to print it, like
    `alchemist auth-token`) or None when no auth is configured at all.
    Raises AuthTokenError / ValueError when App creds are configured but
    the mint fails — fail loud rather than silently fall through.
    """
    if config.has_app_credentials:
        private_key = config.resolve_app_private_key()
        minted = mint_installation_token(
            app_id=config.app_id or "",
            private_key_pem=private_key,
            installation_id=config.app_installation_id or "",
        )
        os.environ[config.github_token_env] = minted.token
        return minted.token
    return os.environ.get(config.github_token_env)


@click.group(invoke_without_command=False)
@click.version_option(version=__version__, prog_name="alchemist")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Alchemist — issue-driven transmuter for the Autumn Garage family."""
    ctx.ensure_object(dict)


@main.command()
def banner() -> None:
    """Print the brand banner (subtitle = tagline)."""
    print_banner(subtitle=SUBTITLE_TAGLINE, version=__version__)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human text.")
def doctor(as_json: bool) -> None:
    """Verify CLIs, auth, and writable state."""
    config = load_config()
    if not as_json:
        print_banner(subtitle=SUBTITLE_DOCTOR, version=__version__)

    # Mint and export the App installation token before running checks so
    # the gh-auth probe sees it. Failures surface as a "github auth" check
    # below rather than as an early raise.
    with contextlib.suppress(AuthTokenError, ValueError):
        _resolve_github_token(config)

    checks = run_doctor(config)

    if as_json:
        payload = {
            "version": __version__,
            "config": {
                "org": config.org,
                "intake_label": config.intake_label,
                "state_label_prefix": config.state_label_prefix,
                "agent_provider": config.agent_provider,
                "agent_stale_after_hours": config.agent_stale_after_hours,
                "auto_merge": config.auto_merge,
                "dry_run": config.dry_run,
                "max_issues_per_tick": config.max_issues_per_tick,
                "max_per_repo_per_tick": config.max_per_repo_per_tick,
                "max_concurrent_repos": config.max_concurrent_repos,
                "state_dir": str(config.state_dir),
            },
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            "ok": all(c.ok for c in checks),
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"  org:             {config.org}")
        click.echo(f"  intake label:    {config.intake_label}")
        click.echo(f"  state prefix:    {config.state_label_prefix}")
        click.echo(f"  agent provider:  {config.agent_provider}")
        click.echo(f"  stale after:     {config.agent_stale_after_hours}h")
        click.echo(f"  auto merge:      {config.auto_merge}")
        click.echo(f"  dry run:         {config.dry_run}")
        click.echo(f"  max issues:      {config.max_issues_per_tick}")
        click.echo(f"  max per repo:    {config.max_per_repo_per_tick}")
        click.echo(f"  max repos:       {config.max_concurrent_repos}")
        click.echo(f"  state dir:       {config.state_dir}")
        click.echo("")
        for c in checks:
            mark = "✓" if c.ok else "✗"
            click.echo(f"  {mark} {c.name:<30} {c.detail}")

    sys.exit(0 if all(c.ok for c in checks) else 1)


@main.command("scan")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human text.")
def scan_cmd(as_json: bool) -> None:
    """Scan the configured org for open issues. No side effects."""
    config = load_config()
    if not as_json:
        print_banner(subtitle=SUBTITLE_SCAN, version=__version__)

    try:
        issues = scan(org=config.org, label=config.intake_label)
    except ScanError as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}), err=True)
        else:
            click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)

    if as_json:
        payload = {
            "org": config.org,
            "intake_label": config.intake_label,
            "state_label_prefix": config.state_label_prefix,
            "count": len(issues),
            "issues": [
                {
                    "number": i.number,
                    "title": i.title,
                    "url": i.url,
                    "repository": i.repository,
                    "updated_at": i.updated_at,
                    "labels": list(i.labels),
                }
                for i in issues
            ],
        }
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"  org:    {config.org}")
    click.echo(f"  label:  {config.intake_label}")
    click.echo(f"  found:  {len(issues)} issue(s)")
    click.echo("")
    if not issues:
        click.echo("  (no open issues)")
        return
    for i in issues:
        click.echo(f"  • {i.repository}#{i.number}  {i.title}")
        click.echo(f"      {i.url}")


@main.command(name="run-once")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human text.")
def run_once(as_json: bool) -> None:
    """Process one tick: scan, dispatch, and babysit linked PRs."""
    from dataclasses import asdict

    from alchemist.banner import SUBTITLE_TAGLINE
    from alchemist.runner import run_tick

    config = load_config()
    if not as_json:
        print_banner(subtitle=f"tick · {SUBTITLE_TAGLINE}", version=__version__)

    # Mint and export the App installation token before run_tick so that
    # run_doctor (and downstream `gh` calls) see it. Mint
    # failures fall through to the doctor check, which surfaces them with
    # context — better than crashing the tick before doctor runs.
    with contextlib.suppress(AuthTokenError, ValueError):
        _resolve_github_token(config)

    results = run_tick(config)

    if as_json:
        click.echo(json.dumps([asdict(r) for r in results], default=str, indent=2))
    else:
        if not results:
            click.echo("  (no work this tick)")
        for r in results:
            mark = "✗" if r.error else "✓"
            tag = "[DRY-RUN]" if r.dry_run else "[LIVE]"
            line = f"  {mark} {tag} {r.repo}#{r.issue_number}  [{r.status}]"
            if r.pr_url:
                merge_tag = "merged" if r.merged else "open" if r.merged is False else "?"
                line += f"  → {r.pr_url}  [{merge_tag}]"
            elif r.error:
                line += f"  error={r.error}"
            line += f"  ({r.elapsed_sec:.1f}s)"
            click.echo(line)

    # Exit non-zero only for run-level failures that prevented the tick from
    # operating (doctor/auth/config/scan). Per-issue failures are handled by
    # transitioning labels/comments/reporting and should not crash cron.
    fatal = [r for r in results if r.error and r.issue_number == 0]
    sys.exit(1 if fatal else 0)


@main.command("auth-token")
def auth_token_cmd() -> None:
    """Print a GitHub token to stdout — debug helper for App auth setup.

    Mints an installation token from App credentials when configured;
    otherwise echoes `$GITHUB_TOKEN` for the v0.1 PAT path. Production
    cron firings don't need this — the runtime resolves auth internally
    in `run-once` and `doctor`. This subcommand exists to verify creds
    are wired up correctly without running a full tick.
    """
    config = load_config()
    try:
        token = _resolve_github_token(config)
    except (AuthTokenError, ValueError) as exc:
        click.echo(f"alchemist auth-token: {exc}", err=True)
        sys.exit(1)
    if not token:
        click.echo(
            f"alchemist auth-token: no App credentials and ${config.github_token_env} unset",
            err=True,
        )
        sys.exit(1)
    click.echo(token)


if __name__ == "__main__":  # pragma: no cover
    main()
