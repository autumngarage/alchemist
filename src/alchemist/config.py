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
    intake_label: str
    state_label_prefix: str
    agent_provider: str
    poll_interval_minutes: int
    state_dir: Path
    dry_run: bool
    max_issues_per_tick: int  # global issue cap per scheduler tick
    max_per_repo_per_tick: int  # how many issues to take from any one repo per tick
    max_concurrent_repos: int  # how many repos to fan out across in parallel
    agent_stale_after_hours: int
    auto_merge: bool
    devin_api_key_env: str
    devin_org_id: str
    github_token_env: str
    assignee_user: str  # GitHub username/login to assign to claimed issues
    repo_blocklist: tuple[str, ...]  # repos in the org to skip during intake

    # GitHub App auth (v0.2 / alchemist#6). When all three are present alchemist
    # mints a per-tick installation token; otherwise it falls back to the PAT
    # in `github_token_env`. Either app_private_key (PEM contents, suited to
    # Railway env vars) or app_private_key_path (filesystem path, suited to
    # local dev) supplies the signing key.
    app_id: str | None
    app_installation_id: str | None
    app_private_key: str | None
    app_private_key_path: Path | None

    @property
    def github_token(self) -> str | None:
        return os.environ.get(self.github_token_env)

    @property
    def has_app_credentials(self) -> bool:
        return bool(
            self.app_id
            and self.app_installation_id
            and (self.app_private_key or self.app_private_key_path)
        )

    def resolve_app_private_key(self) -> str:
        """Return the App private key PEM contents, reading from disk if needed.

        Raises ValueError when no key is configured or the path is unreadable.
        """
        if self.app_private_key:
            return self.app_private_key
        if self.app_private_key_path is None:
            raise ValueError("no App private key configured")
        path = self.app_private_key_path.expanduser()
        try:
            return path.read_text()
        except OSError as exc:
            raise ValueError(f"cannot read App private key at {path}: {exc}") from exc


_DEFAULTS: dict[str, object] = {
    "org": "autumngarage",
    "intake_label": "agent-ready",
    "state_label_prefix": "alchemist",
    "agent_provider": "codex",
    "poll_interval_minutes": 5,
    "state_dir": "/var/alchemist/state",
    "dry_run": True,
    # Bounded blast radius for the dogfood period: 1 issue total, 1 issue per
    # repo, 1 repo at a time. After dogfood B is clean, lift these caps to
    # enable cross-repo swarm without changing code.
    "max_issues_per_tick": 1,
    "max_per_repo_per_tick": 1,
    "max_concurrent_repos": 1,
    "agent_stale_after_hours": 24,
    "auto_merge": False,
    "devin_api_key_env": "DEVIN_API_KEY",
    "devin_org_id": "",
    "github_token_env": "GITHUB_TOKEN",
    "assignee_user": "@me",
    # Comma-separated repo names ("owner/name" or just "name" within the
    # configured org) to skip even when they have eligible open issues. For
    # repos that need local testing, customer-sensitive repos, or anything else
    # alchemist shouldn't touch. Stored as a tuple in the resolved Config; the
    # env-var override is comma-separated.
    "repo_blocklist": "",
    # GitHub App auth — empty by default; v0.2 deployments fill these in.
    "app_id": "",
    "app_installation_id": "",
    "app_private_key": "",
    "app_private_key_path": "",
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


def _coerce_agent_provider(raw: object) -> str:
    provider = str(raw).strip().lower()
    if provider in {"codex", "devin"}:
        return provider
    raise ValueError("agent_provider must be one of codex or devin")


def _coerce_repo_blocklist(raw: object, org: str) -> tuple[str, ...]:
    """Normalize a TOML list or comma-separated env-var into a tuple of
    fully-qualified `owner/name` strings.

    Bare repo names (no slash) are interpreted as `<org>/<name>` so the
    config keeps working when the operator types just `vesper` instead of
    `autumngarage/vesper`.
    """
    if isinstance(raw, (list, tuple)):
        names = [str(item).strip() for item in raw if str(item).strip()]
    else:
        names = [piece.strip() for piece in str(raw).split(",") if piece.strip()]
    qualified = tuple(name if "/" in name else f"{org}/{name}" for name in names)
    return qualified


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
        "ALCHEMIST_INTAKE_LABEL": "intake_label",
        "ALCHEMIST_LABEL": "state_label_prefix",
        "ALCHEMIST_STATE_LABEL_PREFIX": "state_label_prefix",
        "ALCHEMIST_PROVIDER": "agent_provider",
        "ALCHEMIST_AGENT_PROVIDER": "agent_provider",
        "ALCHEMIST_POLL_INTERVAL_MINUTES": "poll_interval_minutes",
        "ALCHEMIST_STATE_DIR": "state_dir",
        "ALCHEMIST_DRY_RUN": "dry_run",
        "ALCHEMIST_MAX_ISSUES_PER_TICK": "max_issues_per_tick",
        "ALCHEMIST_MAX_PER_REPO_PER_TICK": "max_per_repo_per_tick",
        "ALCHEMIST_MAX_CONCURRENT_REPOS": "max_concurrent_repos",
        "ALCHEMIST_AGENT_STALE_AFTER_HOURS": "agent_stale_after_hours",
        "ALCHEMIST_AUTO_MERGE": "auto_merge",
        "ALCHEMIST_DEVIN_API_KEY_ENV": "devin_api_key_env",
        "ALCHEMIST_DEVIN_ORG_ID": "devin_org_id",
        "ALCHEMIST_GITHUB_TOKEN_ENV": "github_token_env",
        "ALCHEMIST_ASSIGNEE": "assignee_user",
        "ALCHEMIST_REPO_BLOCKLIST": "repo_blocklist",
        "ALCHEMIST_APP_ID": "app_id",
        "ALCHEMIST_APP_INSTALLATION_ID": "app_installation_id",
        "ALCHEMIST_APP_PRIVATE_KEY": "app_private_key",
        "ALCHEMIST_APP_PRIVATE_KEY_PATH": "app_private_key_path",
    }
    for env_name, key in env_overrides.items():
        if env_name in os.environ:
            merged[key] = os.environ[env_name]

    return Config(
        org=str(merged["org"]),
        intake_label=str(merged["intake_label"]),
        state_label_prefix=str(merged["state_label_prefix"]),
        agent_provider=_coerce_agent_provider(merged["agent_provider"]),
        poll_interval_minutes=_coerce_int(merged["poll_interval_minutes"]),
        state_dir=Path(str(merged["state_dir"])),
        dry_run=_coerce_bool(merged["dry_run"]),
        max_issues_per_tick=_coerce_int(merged["max_issues_per_tick"]),
        max_per_repo_per_tick=_coerce_int(merged["max_per_repo_per_tick"]),
        max_concurrent_repos=_coerce_int(merged["max_concurrent_repos"]),
        agent_stale_after_hours=_coerce_int(merged["agent_stale_after_hours"]),
        auto_merge=_coerce_bool(merged["auto_merge"]),
        devin_api_key_env=str(merged["devin_api_key_env"]),
        devin_org_id=str(merged["devin_org_id"]).strip(),
        github_token_env=str(merged["github_token_env"]),
        assignee_user=str(merged["assignee_user"]),
        repo_blocklist=_coerce_repo_blocklist(merged["repo_blocklist"], str(merged["org"])),
        app_id=str(merged["app_id"]).strip() or None,
        app_installation_id=str(merged["app_installation_id"]).strip() or None,
        app_private_key=str(merged["app_private_key"]) or None,
        app_private_key_path=(
            Path(str(merged["app_private_key_path"])).expanduser()
            if str(merged["app_private_key_path"]).strip()
            else None
        ),
    )
