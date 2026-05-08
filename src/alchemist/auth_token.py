"""Mint a GitHub App installation access token.

Alchemist v0.2 swaps the static fine-grained PAT for a per-tick installation
token minted from the `autumn-alchemist` GitHub App's credentials. The
runtime resolves auth via this module; the rest of alchemist still consumes
`GITHUB_TOKEN` from the environment unchanged.

Flow:

1. Sign a short-lived JWT (RS256) with the App's private key.
2. POST it to `/app/installations/{id}/access_tokens`.
3. Return the resulting installation token (valid ~1 hour).

The cron entrypoint wraps with `GITHUB_TOKEN=$(alchemist auth-token) …` so
each tick gets a fresh token; we don't bother caching across ticks.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import jwt

GITHUB_API = "https://api.github.com"
JWT_LIFETIME_SEC = 540  # 9 min — under GitHub's 10-min ceiling, with margin
JWT_BACKDATE_SEC = 60  # tolerate small clock skew between us and GitHub


class AuthTokenError(RuntimeError):
    """Failure minting an installation token. Message is operator-actionable."""


@dataclass(frozen=True)
class InstallationToken:
    token: str
    expires_at: str  # ISO 8601, GitHub-formatted


def _build_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    issued_at = (now if now is not None else int(time.time())) - JWT_BACKDATE_SEC
    payload = {
        "iat": issued_at,
        "exp": issued_at + JWT_BACKDATE_SEC + JWT_LIFETIME_SEC,
        "iss": app_id,
    }
    try:
        return jwt.encode(payload, private_key_pem, algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError) as exc:
        raise AuthTokenError(f"could not sign App JWT (bad private key?): {exc}") from exc


def _post_installation_access_token(
    app_jwt: str,
    installation_id: str,
    *,
    opener: urllib.request.OpenerDirector | None = None,
) -> InstallationToken:
    url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
    request = urllib.request.Request(  # noqa: S310 — fixed https URL
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "alchemist-auth-token",
        },
    )
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_fn(request, timeout=15) as response:  # noqa: S310 — fixed https URL
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise AuthTokenError(
            f"GitHub rejected installation-token request "
            f"(HTTP {exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AuthTokenError(f"network failure minting installation token: {exc}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AuthTokenError(f"GitHub returned non-JSON: {body[:200]}") from exc

    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not isinstance(token, str) or not isinstance(expires_at, str):
        raise AuthTokenError(f"GitHub response missing token/expires_at: {payload!r}")
    return InstallationToken(token=token, expires_at=expires_at)


def mint_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: str,
    *,
    now: int | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> InstallationToken:
    """Mint a fresh installation access token for the given App + installation.

    `now` and `opener` are seams for tests — production callers pass nothing.
    """
    if not app_id or not installation_id:
        raise AuthTokenError("app_id and installation_id are required")
    if not private_key_pem.strip():
        raise AuthTokenError("private_key_pem is empty")

    app_jwt = _build_app_jwt(app_id, private_key_pem, now=now)
    return _post_installation_access_token(app_jwt, installation_id, opener=opener)
