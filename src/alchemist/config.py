"""Configuration loader for Alchemist.

Resolution order: built-in defaults < TOML file < environment variables.
The env-var layer wins so deployments (Railway) can flip dogfood gates
without rewriting the config file in the image.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATHS = (
    Path("/etc/alchemist/config.toml"),
    Path.home() / ".alchemist" / "config.toml",
)


@dataclass(frozen=True)
class Config:
    """Resolved configuration for one alchemist deployment.

    One deployment scopes to one GitHub org. Multi-org operators run
    multiple deployments, each with its own config + GitHub token.
    """

    org: str
    dispatch_label: str
    default_provider: str
    default_budget: str
    poll_interval_minutes: int
    state_dir: Path
    dry_run: bool
    max_per_repo_per_tick: int   # how many issues to take from any one repo per tick
    max_concurrent_repos: int     # how many repos to fan out across in parallel
    conductor_timeout_sec: int
    review_timeout_sec: int
    github_token_env: str

    @property
    def github_token(self) -> str | None:
        return os.environ.get(self.github_token_env)


_DEFAULTS: dict[str, object] = {
    "org": "autumngarage",
    "dispatch_label": "alchemist-test",
    # `openrouter` is the headless default: env-var keyed (OPENROUTER_API_KEY)
    # AND `tools=all` so the agentic loop can Read/Edit/Write/Bash. `claude` and
    # `codex` use OAuth-via-local-CLI and won't work in a container; `kimi` and
    # `deepseek-*` via OpenRouter don't expose tools. Override per-deployment via
    # ALCHEMIST_PROVIDER.
    "default_provider": "openrouter",
    "default_budget": "$2",
    "poll_interval_minutes": 5,
    "state_dir": "/var/alchemist/state",
    "dry_run": True,
    # Bounded blast radius for the dogfood period: 1 issue per repo, 1 repo at
    # a time. After dogfood B is clean, lift max_concurrent_repos to e.g. 3
    # to enable cross-repo swarm.
    "max_per_repo_per_tick": 1,
    "max_concurrent_repos": 1,
    "conductor_timeout_sec": 600,
    "review_timeout_sec": 300,
    "github_token_env": "GITHUB_TOKEN",
}


def _coerce_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _coerce_int(raw: object) -> int:
    if isinstance(raw, int):
        return raw
    return int(str(raw).strip())


def _config_path() -> Path | None:
    """Return an existing config path, or None when no config file is in play.

    Resolution: $ALCHEMIST_CONFIG (if set and existing), then the default
    paths in order. Missing paths are treated as 'no config file' rather
    than as errors so deployments that drive everything via env vars
    (Railway) work without a config file at all.
    """
    explicit = os.environ.get("ALCHEMIST_CONFIG")
    if explicit:
        explicit_path = Path(explicit)
        return explicit_path if explicit_path.exists() else None
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    return None


def _load_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    section = data.get("alchemist", {})
    if not isinstance(section, dict):
        raise ValueError(f"{path}: expected [alchemist] table")
    return dict(section)


def load_config() -> Config:
    """Resolve and validate the runtime configuration."""
    merged: dict[str, object] = dict(_DEFAULTS)

    path = _config_path()
    if path is not None:
        merged.update(_load_toml(path))

    env_overrides: dict[str, str] = {
        "ALCHEMIST_ORG": "org",
        "ALCHEMIST_LABEL": "dispatch_label",
        "ALCHEMIST_PROVIDER": "default_provider",
        "ALCHEMIST_BUDGET": "default_budget",
        "ALCHEMIST_POLL_INTERVAL_MINUTES": "poll_interval_minutes",
        "ALCHEMIST_STATE_DIR": "state_dir",
        "ALCHEMIST_DRY_RUN": "dry_run",
        "ALCHEMIST_MAX_PER_REPO_PER_TICK": "max_per_repo_per_tick",
        "ALCHEMIST_MAX_CONCURRENT_REPOS": "max_concurrent_repos",
        "ALCHEMIST_CONDUCTOR_TIMEOUT_SEC": "conductor_timeout_sec",
        "ALCHEMIST_REVIEW_TIMEOUT_SEC": "review_timeout_sec",
        "ALCHEMIST_GITHUB_TOKEN_ENV": "github_token_env",
    }
    for env_name, key in env_overrides.items():
        if env_name in os.environ:
            merged[key] = os.environ[env_name]

    return Config(
        org=str(merged["org"]),
        dispatch_label=str(merged["dispatch_label"]),
        default_provider=str(merged["default_provider"]),
        default_budget=str(merged["default_budget"]),
        poll_interval_minutes=_coerce_int(merged["poll_interval_minutes"]),
        state_dir=Path(str(merged["state_dir"])),
        dry_run=_coerce_bool(merged["dry_run"]),
        max_per_repo_per_tick=_coerce_int(merged["max_per_repo_per_tick"]),
        max_concurrent_repos=_coerce_int(merged["max_concurrent_repos"]),
        conductor_timeout_sec=_coerce_int(merged["conductor_timeout_sec"]),
        review_timeout_sec=_coerce_int(merged["review_timeout_sec"]),
        github_token_env=str(merged["github_token_env"]),
    )
