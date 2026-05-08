"""Tests for the config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.config import Config, load_config


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Strip ALCHEMIST_* env vars and point ALCHEMIST_CONFIG at tmp."""
    import os
    for key in [k for k in os.environ if k.startswith("ALCHEMIST_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    yield


def test_defaults_when_no_config_file_or_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    cfg = load_config()
    assert cfg.org == "autumngarage"
    assert cfg.dispatch_label == "alchemist-test"
    assert cfg.dry_run is True
    assert cfg.max_per_repo_per_tick == 1
    assert cfg.max_concurrent_repos == 1
    assert cfg.repo_blocklist == ()


def test_repo_blocklist_env_var_comma_separated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ALCHEMIST_REPO_BLOCKLIST", "vesper,autumngarage/secret,foo/bar")
    cfg = load_config()
    # Bare names get qualified with the configured org; already-qualified pass through.
    assert cfg.repo_blocklist == (
        "autumngarage/vesper",
        "autumngarage/secret",
        "foo/bar",
    )


def test_repo_blocklist_toml_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "alchemist.toml"
    cfg_file.write_text(
        """
[alchemist]
org = "autumngarage"
repo_blocklist = ["vesper", "henrymodisett/private"]
"""
    )
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(cfg_file))
    cfg = load_config()
    assert cfg.repo_blocklist == (
        "autumngarage/vesper",
        "henrymodisett/private",
    )


def test_repo_blocklist_empty_string_is_empty_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ALCHEMIST_REPO_BLOCKLIST", "")
    cfg = load_config()
    assert cfg.repo_blocklist == ()


def test_toml_overrides_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "alchemist.toml"
    cfg_file.write_text(
        """
[alchemist]
org = "henrymodisett"
dispatch_label = "alchemist-dispatch"
dry_run = false
max_per_repo_per_tick = 3
max_concurrent_repos = 5
"""
    )
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(cfg_file))
    cfg = load_config()
    assert cfg.org == "henrymodisett"
    assert cfg.dispatch_label == "alchemist-dispatch"
    assert cfg.dry_run is False
    assert cfg.max_per_repo_per_tick == 3
    assert cfg.max_concurrent_repos == 5


def test_env_var_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "alchemist.toml"
    cfg_file.write_text(
        """
[alchemist]
org = "from-toml"
dry_run = false
"""
    )
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(cfg_file))
    monkeypatch.setenv("ALCHEMIST_ORG", "from-env")
    monkeypatch.setenv("ALCHEMIST_DRY_RUN", "true")
    cfg = load_config()
    assert cfg.org == "from-env"
    assert cfg.dry_run is True


def test_dry_run_string_coercion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ALCHEMIST_DRY_RUN", "false")
    cfg = load_config()
    assert cfg.dry_run is False


def test_config_is_frozen():
    """Config dataclass is immutable so no surprise mutations across the runner."""
    cfg = Config(
        org="x",
        dispatch_label="y",
        default_provider="kimi",
        default_budget="$1",
        poll_interval_minutes=5,
        state_dir=Path("/tmp"),
        dry_run=True,
        max_per_repo_per_tick=1,
        max_concurrent_repos=1,
        conductor_timeout_sec=600,
        review_timeout_sec=300,
        github_token_env="GITHUB_TOKEN",
        assignee_user="@me",
        repo_blocklist=(),
        app_id=None,
        app_installation_id=None,
        app_private_key=None,
        app_private_key_path=None,
    )
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        cfg.org = "mutated"  # type: ignore[misc]


def test_app_credentials_unset_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    cfg = load_config()
    assert cfg.app_id is None
    assert cfg.app_installation_id is None
    assert cfg.app_private_key is None
    assert cfg.app_private_key_path is None
    assert cfg.has_app_credentials is False


def test_app_credentials_loaded_from_env_with_inline_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ALCHEMIST_APP_ID", "3628230")
    monkeypatch.setenv("ALCHEMIST_APP_INSTALLATION_ID", "130170611")
    monkeypatch.setenv(
        "ALCHEMIST_APP_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    )
    cfg = load_config()
    assert cfg.app_id == "3628230"
    assert cfg.app_installation_id == "130170611"
    assert cfg.has_app_credentials is True
    assert cfg.resolve_app_private_key().startswith("-----BEGIN PRIVATE KEY-----")


def test_app_credentials_via_path_reads_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    key_file = tmp_path / "app.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nfromdisk\n-----END PRIVATE KEY-----")
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ALCHEMIST_APP_ID", "3628230")
    monkeypatch.setenv("ALCHEMIST_APP_INSTALLATION_ID", "130170611")
    monkeypatch.setenv("ALCHEMIST_APP_PRIVATE_KEY_PATH", str(key_file))
    cfg = load_config()
    assert cfg.has_app_credentials is True
    assert "fromdisk" in cfg.resolve_app_private_key()


def test_has_app_credentials_requires_all_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two of three env vars set → not enough; we don't half-mint."""
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ALCHEMIST_APP_ID", "3628230")
    monkeypatch.setenv("ALCHEMIST_APP_INSTALLATION_ID", "130170611")
    # No private key → falls back to PAT path.
    cfg = load_config()
    assert cfg.has_app_credentials is False


def test_resolve_app_private_key_raises_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(tmp_path / "missing.toml"))
    cfg = load_config()
    with pytest.raises(ValueError):
        cfg.resolve_app_private_key()
