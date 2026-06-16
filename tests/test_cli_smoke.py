"""CLI smoke — confirms the top-level surface boots without import errors."""

from __future__ import annotations

import os

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
    assert "auth-token" in result.output


def test_auth_token_passes_through_pat_when_no_app_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_pat")
    runner = CliRunner()
    result = runner.invoke(main, ["auth-token"])
    assert result.exit_code == 0
    assert result.output.strip() == "ghp_fake_pat"


def test_auth_token_exits_one_when_no_creds_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["auth-token"])
    assert result.exit_code == 1
    assert "no App credentials" in result.output or "no App credentials" in (
        result.stderr if result.stderr_bytes else ""
    )


def test_auth_token_mints_when_app_creds_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setenv("ALCHEMIST_APP_ID", "3628230")
    monkeypatch.setenv("ALCHEMIST_APP_INSTALLATION_ID", "130170611")
    monkeypatch.setenv("ALCHEMIST_APP_PRIVATE_KEY", "fake-pem")

    from alchemist.auth_token import InstallationToken

    def _fake_mint(*, app_id, private_key_pem, installation_id):
        assert app_id == "3628230"
        assert installation_id == "130170611"
        assert private_key_pem == "fake-pem"
        return InstallationToken(token="ghs_minted", expires_at="2030-01-01T00:00:00Z")

    monkeypatch.setattr("alchemist.cli.mint_installation_token", _fake_mint)

    runner = CliRunner()
    result = runner.invoke(main, ["auth-token"])
    assert result.exit_code == 0
    assert result.output.strip() == "ghs_minted"


def test_run_once_sets_minted_token_in_env_for_subprocess_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """run-once must export the minted installation token so `gh` and
    `git push` see it. Regression guard: relying on an external shell
    wrapper bit us in alchemist#6 when railway.json's startCommand
    overrode the Dockerfile CMD.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ALCHEMIST_APP_ID", "3628230")
    monkeypatch.setenv("ALCHEMIST_APP_INSTALLATION_ID", "130170611")
    monkeypatch.setenv("ALCHEMIST_APP_PRIVATE_KEY", "fake-pem")
    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))

    from alchemist.auth_token import InstallationToken

    monkeypatch.setattr(
        "alchemist.cli.mint_installation_token",
        lambda **_: InstallationToken(
            token="ghs_minted_in_run_once", expires_at="2030-01-01T00:00:00Z"
        ),
    )

    captured: dict[str, str | None] = {}

    def _fake_run_tick(config):
        captured["GITHUB_TOKEN"] = os.environ.get("GITHUB_TOKEN")
        return []

    monkeypatch.setattr("alchemist.runner.run_tick", _fake_run_tick)

    runner = CliRunner()
    result = runner.invoke(main, ["run-once", "--json"])
    assert result.exit_code == 0
    assert captured["GITHUB_TOKEN"] == "ghs_minted_in_run_once"


def test_doctor_sets_minted_token_in_env_before_checks_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ALCHEMIST_APP_ID", "3628230")
    monkeypatch.setenv("ALCHEMIST_APP_INSTALLATION_ID", "130170611")
    monkeypatch.setenv("ALCHEMIST_APP_PRIVATE_KEY", "fake-pem")
    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))

    from alchemist.auth_token import InstallationToken

    monkeypatch.setattr(
        "alchemist.cli.mint_installation_token",
        lambda **_: InstallationToken(
            token="ghs_minted_in_doctor", expires_at="2030-01-01T00:00:00Z"
        ),
    )

    seen: dict[str, str | None] = {}

    def _fake_run_doctor(config):
        seen["GITHUB_TOKEN"] = os.environ.get("GITHUB_TOKEN")
        from alchemist.doctor import Check
        return [Check(name="github auth", ok=True, detail="stub")]

    monkeypatch.setattr("alchemist.cli.run_doctor", _fake_run_doctor)

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0
    assert seen["GITHUB_TOKEN"] == "ghs_minted_in_doctor"


def test_banner_subcommand_prints_attribution():
    runner = CliRunner()
    result = runner.invoke(main, ["banner"])
    assert result.exit_code == 0
    out = result.output + (result.stderr if result.stderr_bytes else "")
    # Banner goes to stderr; click's CliRunner mixes the streams by default.
    assert "Autumn Garage" in out


def test_run_once_json_with_no_work(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setattr("alchemist.runner.run_tick", lambda config: [])

    runner = CliRunner()
    result = runner.invoke(main, ["run-once", "--json"])
    assert result.exit_code == 0
    assert result.output.strip() == "[]"


def test_run_once_text_with_no_work(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setattr("alchemist.runner.run_tick", lambda config: [])

    runner = CliRunner()
    result = runner.invoke(main, ["run-once"])
    assert result.exit_code == 0
    assert "no work this tick" in result.output


def test_run_once_exits_zero_when_all_errors_are_benign(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """Issue-level waiting/lock states are non-fatal for cron."""
    from alchemist.runner import RunResult

    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    benign = [
        RunResult(
            repo="autumngarage/widgets",
            issue_number=1,
            pr_url=None,
            merged=None,
            error=None,
            elapsed_sec=0.1,
            dry_run=True,
            status="waiting",
        ),
        RunResult(
            repo="autumngarage/cortex",
            issue_number=2,
            pr_url=None,
            merged=None,
            error="lock-busy: /var/.../locks/autumngarage-cortex.lock",
            elapsed_sec=0.0,
            dry_run=True,
            status="lock-busy",
        ),
    ]
    monkeypatch.setattr("alchemist.runner.run_tick", lambda config: benign)

    runner = CliRunner()
    result = runner.invoke(main, ["run-once", "--json"])
    assert result.exit_code == 0


def test_run_once_exits_zero_when_issue_level_tool_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    from alchemist.runner import RunResult

    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    handled = [
        RunResult(
            repo="autumngarage/widgets",
            issue_number=1,
            pr_url=None,
            merged=None,
            error="agent dispatch failed",
            elapsed_sec=600.0,
            dry_run=False,
            status="error",
        )
    ]
    monkeypatch.setattr("alchemist.runner.run_tick", lambda config: handled)

    runner = CliRunner()
    result = runner.invoke(main, ["run-once", "--json"])
    assert result.exit_code == 0


def test_run_once_exits_one_when_run_level_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    from alchemist.runner import RunResult

    monkeypatch.setenv("ALCHEMIST_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    fatal = [
        RunResult(
            repo="autumngarage",
            issue_number=0,
            pr_url=None,
            merged=None,
            error="doctor: github auth: missing",
            elapsed_sec=0.0,
            dry_run=False,
            status="fatal",
        )
    ]
    monkeypatch.setattr("alchemist.runner.run_tick", lambda config: fatal)

    runner = CliRunner()
    result = runner.invoke(main, ["run-once", "--json"])
    assert result.exit_code == 1
