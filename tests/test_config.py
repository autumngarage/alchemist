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
    assert cfg.max_per_tick == 1


def test_toml_overrides_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "alchemist.toml"
    cfg_file.write_text(
        """
[alchemist]
org = "henrymodisett"
dispatch_label = "alchemist-dispatch"
dry_run = false
max_per_tick = 3
"""
    )
    monkeypatch.setenv("ALCHEMIST_CONFIG", str(cfg_file))
    cfg = load_config()
    assert cfg.org == "henrymodisett"
    assert cfg.dispatch_label == "alchemist-dispatch"
    assert cfg.dry_run is False
    assert cfg.max_per_tick == 3


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
        max_per_tick=1,
        conductor_timeout_sec=600,
        review_timeout_sec=300,
        github_token_env="GITHUB_TOKEN",
    )
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        cfg.org = "mutated"  # type: ignore[misc]
