"""Regression tests for the Railway cron entrypoint."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def test_railway_entrypoint_runs_tick_without_pat(tmp_path: Path):
    """GitHub App deployments do not have a static GITHUB_TOKEN at startup."""
    repo_root = Path(__file__).resolve().parents[1]
    fake_alchemist = tmp_path / "alchemist"
    fake_alchemist.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'alchemist %s\\n' \"$*\"\n",
    )
    fake_alchemist.chmod(0o755)

    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(  # noqa: S603
        ["bash", "scripts/railway-entrypoint.sh"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "Starting Alchemist Tick" in result.stdout
    assert "alchemist run-once --json" in result.stdout
    assert "Tool Refresh" not in result.stdout


def test_railway_image_uses_uv_new_enough_for_dynamic_project_versions():
    """Old uv builds failed `uv sync` on hatch-vcs/dynamic-version projects."""
    repo_root = Path(__file__).resolve().parents[1]
    dockerfile = (repo_root / "Dockerfile").read_text()
    match = re.search(r"^ARG UV_VERSION=([0-9]+)\.([0-9]+)\.([0-9]+)$", dockerfile, re.M)

    assert match is not None
    major, minor, patch = (int(part) for part in match.groups())
    assert (major, minor, patch) >= (0, 11, 0)
