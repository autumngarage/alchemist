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
                "max_per_tick": config.max_per_tick,
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
        click.echo(f"  max per tick:    {config.max_per_tick}")
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
def run_once() -> None:
    """Process one tick. NOT YET IMPLEMENTED — ships in v0.1."""
    click.echo(
        "ERROR: `alchemist run-once` ships in v0.1. "
        "v0.0.x exposes scan + doctor only.",
        err=True,
    )
    sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
