"""Health check — verify external CLIs, env vars, and writable state.

Run before each cron tick (and as a standalone command). A clean doctor
output means the next `run-once` tick has everything it needs to make
forward progress; a failed doctor short-circuits the tick with a clear
error rather than silently producing a half-built PR.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


def _gh_auth_check(token_env: str) -> Check:
    if not os.environ.get(token_env):
        return Check(
            name="github auth",
            ok=False,
            detail=f"${token_env} not set",
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


def _touchstone_merge_pr_script_check() -> Check:
    """Locate touchstone's merge-pr.sh — alchemist's review-and-merge gate.

    Looks at $TOUCHSTONE_ROOT first, then `brew --prefix touchstone`/libexec,
    then /opt/touchstone (Linux container convention).
    """
    candidates: list[Path] = []
    env_root = os.environ.get("TOUCHSTONE_ROOT")
    if env_root:
        candidates.append(Path(env_root) / "scripts" / "merge-pr.sh")
        candidates.append(Path(env_root) / "libexec" / "scripts" / "merge-pr.sh")

    try:
        result = subprocess.run(  # noqa: S603,S607 — brew is on PATH; deterministic args
            ["brew", "--prefix", "touchstone"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            brew_root = Path(result.stdout.strip())
            candidates.append(brew_root / "libexec" / "scripts" / "merge-pr.sh")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    candidates.append(Path("/opt/touchstone/scripts/merge-pr.sh"))
    candidates.append(Path("/opt/touchstone/libexec/scripts/merge-pr.sh"))

    for candidate in candidates:
        if candidate.exists():
            return Check(
                name="touchstone merge-pr.sh",
                ok=True,
                detail=f"found at {candidate}",
            )
    return Check(
        name="touchstone merge-pr.sh",
        ok=False,
        detail="not found — set $TOUCHSTONE_ROOT or install via brew",
    )


def run_doctor(config: Config) -> list[Check]:
    """Run all health checks and return the results."""
    return [
        _which_check("gh", "install GitHub CLI: https://cli.github.com/"),
        _which_check("git", "git is required"),
        _which_check("conductor", "brew install autumngarage/conductor/conductor"),
        _which_check("touchstone", "brew install autumngarage/touchstone/touchstone"),
        _gh_auth_check(config.github_token_env),
        _state_dir_check(config.state_dir),
        _touchstone_merge_pr_script_check(),
    ]
