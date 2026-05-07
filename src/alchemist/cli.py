"""CLI entry point for alchemist."""

from __future__ import annotations

import json
import sys

import click

from alchemist import __version__
from alchemist.banner import (
    SUBTITLE_DOCTOR,
    SUBTITLE_SCAN,
    SUBTITLE_TAGLINE,
    print_banner,
)
from alchemist.config import load_config
from alchemist.doctor import run_doctor
from alchemist.scanner import ScanError, scan


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

    checks = run_doctor(config)

    if as_json:
        payload = {
            "version": __version__,
            "config": {
                "org": config.org,
                "dispatch_label": config.dispatch_label,
                "default_provider": config.default_provider,
                "dry_run": config.dry_run,
                "max_per_repo_per_tick": config.max_per_repo_per_tick,
                "max_concurrent_repos": config.max_concurrent_repos,
                "state_dir": str(config.state_dir),
            },
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks
            ],
            "ok": all(c.ok for c in checks),
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"  org:             {config.org}")
        click.echo(f"  dispatch label:  {config.dispatch_label}")
        click.echo(f"  default provider: {config.default_provider}")
        click.echo(f"  dry run:         {config.dry_run}")
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
    """Scan the configured org for labelled issues. No side effects."""
    config = load_config()
    if not as_json:
        print_banner(subtitle=SUBTITLE_SCAN, version=__version__)

    try:
        issues = scan(org=config.org, label=config.dispatch_label)
    except ScanError as exc:
        if as_json:
            click.echo(json.dumps({"error": str(exc)}), err=True)
        else:
            click.echo(f"ERROR: {exc}", err=True)
        sys.exit(2)

    if as_json:
        payload = {
            "org": config.org,
            "label": config.dispatch_label,
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
    click.echo(f"  label:  {config.dispatch_label}")
    click.echo(f"  found:  {len(issues)} issue(s)")
    click.echo("")
    if not issues:
        click.echo("  (no labelled issues)")
        return
    for i in issues:
        click.echo(f"  • {i.repository}#{i.number}  {i.title}")
        click.echo(f"      {i.url}")


@main.command(name="run-once")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human text.")
def run_once(as_json: bool) -> None:
    """Process one tick: scan, fan out across repos, dispatch, review, open PRs."""
    from dataclasses import asdict

    from alchemist.banner import SUBTITLE_TAGLINE
    from alchemist.runner import run_tick

    config = load_config()
    if not as_json:
        print_banner(subtitle=f"tick · {SUBTITLE_TAGLINE}", version=__version__)

    results = run_tick(config)

    if as_json:
        click.echo(json.dumps([asdict(r) for r in results], default=str, indent=2))
    else:
        if not results:
            click.echo("  (no work this tick)")
        for r in results:
            mark = "✗" if r.error else "✓"
            tag = "[DRY-RUN]" if r.dry_run else "[LIVE]"
            line = f"  {mark} {tag} {r.repo}#{r.issue_number}"
            if r.pr_url:
                line += f"  → {r.pr_url}"
            elif r.review_verdict:
                line += f"  review={r.review_verdict}"
            elif r.error:
                line += f"  error={r.error}"
            line += f"  ({r.elapsed_sec:.1f}s)"
            click.echo(line)

    benign_prefixes = ("lock-busy", "conductor produced no diff")
    fatal = [
        r for r in results
        if r.error and not any(r.error.startswith(p) for p in benign_prefixes)
    ]
    sys.exit(1 if fatal else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
