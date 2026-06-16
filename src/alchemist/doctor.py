"""Health check — verify external CLIs, env vars, and writable state.

Run before each cron tick (and as a standalone command). A clean doctor
output means the next `run-once` tick has everything it needs to coordinate
issues and PRs; a failed doctor short-circuits the tick with a clear error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alchemist.auth_token import AuthTokenError, mint_installation_token

if TYPE_CHECKING:
    from pathlib import Path

    from alchemist.config import Config


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _which_check(binary: str, install_hint: str) -> Check:
    found = shutil.which(binary)
    if found:
        return Check(name=binary, ok=True, detail=f"found at {found}")
    return Check(name=binary, ok=False, detail=f"not on PATH — {install_hint}")


def _gh_auth_check(config: Config) -> Check:
    """Verify alchemist can authenticate to GitHub.

    Two paths: with App credentials we attempt a real installation-token mint
    (proves app_id + private_key + installation_id all work). Without, we
    fall through to `gh auth status` against `$GITHUB_TOKEN` — the v0.1 PAT
    path. Either path puts a usable token into the environment for the rest
    of the tick.
    """
    if config.has_app_credentials:
        try:
            private_key = config.resolve_app_private_key()
        except ValueError as exc:
            return Check(name="github auth", ok=False, detail=str(exc))
        try:
            minted = mint_installation_token(
                app_id=config.app_id or "",
                private_key_pem=private_key,
                installation_id=config.app_installation_id or "",
            )
        except AuthTokenError as exc:
            return Check(name="github auth", ok=False, detail=str(exc))
        return Check(
            name="github auth",
            ok=True,
            detail=f"app installation token (expires {minted.expires_at})",
        )

    if not os.environ.get(config.github_token_env):
        return Check(
            name="github auth",
            ok=False,
            detail=f"${config.github_token_env} not set",
        )
    try:
        result = subprocess.run(  # noqa: S603,S607 — gh is on PATH; auth status takes no user input
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return Check(name="github auth", ok=False, detail=f"gh auth status failed: {exc}")
    if result.returncode != 0:
        return Check(
            name="github auth",
            ok=False,
            detail=f"gh auth status exit {result.returncode}: {result.stderr.strip()}",
        )
    return Check(name="github auth", ok=True, detail="authenticated")


def _state_dir_check(path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check(name="state dir", ok=False, detail=f"{path}: {exc}")
    probe = path / ".alchemist-write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check(name="state dir", ok=False, detail=f"{path} not writable: {exc}")
    return Check(name="state dir", ok=True, detail=f"{path} writable")


def _agent_provider_check(config: Config) -> Check:
    if config.agent_provider == "codex":
        return Check(
            name="agent provider",
            ok=True,
            detail="codex via GitHub @codex comments",
        )
    if config.agent_provider == "devin":
        api_key = os.environ.get(config.devin_api_key_env)
        if not api_key:
            return Check(
                name="agent provider",
                ok=False,
                detail=f"${config.devin_api_key_env} not set for Devin API dispatch",
            )
        if not config.devin_org_id:
            return Check(
                name="agent provider",
                ok=False,
                detail="ALCHEMIST_DEVIN_ORG_ID not set for Devin API dispatch",
            )
        return Check(name="agent provider", ok=True, detail="devin API dispatch configured")
    return Check(name="agent provider", ok=False, detail=f"unsupported {config.agent_provider}")


def run_doctor(config: Config) -> list[Check]:
    """Run all health checks and return the results."""
    checks = [
        _which_check("gh", "install GitHub CLI: https://cli.github.com/"),
        _gh_auth_check(config),
        _agent_provider_check(config),
        _state_dir_check(config.state_dir),
    ]
    if "ALCHEMIST_LABEL" in os.environ:
        checks.append(
            Check(
                name="config deprecation",
                ok=True,
                detail=(
                    "ALCHEMIST_LABEL is deprecated — use ALCHEMIST_STATE_LABEL_PREFIX "
                    "for state labels and ALCHEMIST_INTAKE_LABEL for the intake queue."
                ),
            )
        )
    return checks
