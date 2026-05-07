"""CLI smoke — confirms the top-level surface boots without import errors."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from alchemist.cli import main


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import os
    for key in [k for k in os.environ if k.startswith("ALCHEMIST_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))
    yield


def test_help_prints_subcommands():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.output
    assert "doctor" in result.output
    assert "banner" in result.output


def test_banner_subcommand_prints_attribution():
    runner = CliRunner()
    result = runner.invoke(main, ["banner"])
    assert result.exit_code == 0
    out = result.output + (result.stderr if result.stderr_bytes else "")
    # Banner goes to stderr; click's CliRunner mixes the streams by default.
    assert "Autumn Garage" in out


def test_run_once_not_implemented_yet():
    runner = CliRunner()
    result = runner.invoke(main, ["run-once"])
    assert result.exit_code != 0
    assert "v0.1" in result.output or "v0.1" in (result.stderr or "")
