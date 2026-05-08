"""Tests for the GitHub App installation-token mint."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from alchemist.auth_token import (
    AuthTokenError,
    InstallationToken,
    _build_app_jwt,
    mint_installation_token,
)


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, Any]:
    """Generate a throwaway RSA keypair for JWT signing tests."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return pem, private.public_key()


def _fake_opener(
    payload: dict | None = None,
    *,
    http_error: tuple[int, str] | None = None,
    url_error: str | None = None,
) -> urllib.request.OpenerDirector:
    """Build an opener whose .open() returns fake bytes / raises a chosen error."""
    captured: dict = {}

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    class _Opener:
        def open(self, request, timeout=None):  # noqa: A003 — mirroring urllib API
            captured["request"] = request
            captured["timeout"] = timeout
            if http_error is not None:
                code, body = http_error
                raise urllib.error.HTTPError(
                    request.full_url, code, "fail", {}, io.BytesIO(body.encode("utf-8"))
                )
            if url_error is not None:
                raise urllib.error.URLError(url_error)
            return _Resp(json.dumps(payload).encode("utf-8"))

    opener = _Opener()
    # Test seam: stash the captured request on the opener so callers can assert.
    opener._captured = captured  # type: ignore[attr-defined]
    return opener  # type: ignore[return-value]


def test_build_app_jwt_is_signed_with_rs256_and_carries_app_id(rsa_keypair):
    pem, public_key = rsa_keypair
    token = _build_app_jwt("12345", pem, now=1_700_000_000)
    # We're checking signing + payload shape, not freshness — the synthetic
    # `now` is deliberately old to make assertions deterministic.
    decoded = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert decoded["iss"] == "12345"
    # iat is backdated 60s for clock skew.
    assert decoded["iat"] == 1_700_000_000 - 60
    # exp is well within GitHub's 10-min ceiling.
    assert decoded["exp"] - decoded["iat"] <= 10 * 60


def test_build_app_jwt_rejects_bad_private_key():
    with pytest.raises(AuthTokenError):
        _build_app_jwt("12345", "not a real PEM", now=1_700_000_000)


def test_mint_returns_token_and_expiry_on_success(rsa_keypair):
    pem, _ = rsa_keypair
    opener = _fake_opener({"token": "ghs_xxx", "expires_at": "2030-01-01T00:00:00Z"})
    result = mint_installation_token(
        "12345", pem, "9876", now=1_700_000_000, opener=opener
    )
    assert result == InstallationToken(token="ghs_xxx", expires_at="2030-01-01T00:00:00Z")
    captured = opener._captured  # type: ignore[attr-defined]
    request: urllib.request.Request = captured["request"]
    assert request.full_url.endswith("/app/installations/9876/access_tokens")
    assert request.get_method() == "POST"
    assert request.get_header("Authorization", "").startswith("Bearer ")


def test_mint_rejects_missing_inputs(rsa_keypair):
    pem, _ = rsa_keypair
    with pytest.raises(AuthTokenError, match="required"):
        mint_installation_token("", pem, "9876")
    with pytest.raises(AuthTokenError, match="required"):
        mint_installation_token("12345", pem, "")
    with pytest.raises(AuthTokenError, match="empty"):
        mint_installation_token("12345", "   ", "9876")


def test_mint_surfaces_http_error_with_body_snippet(rsa_keypair):
    pem, _ = rsa_keypair
    opener = _fake_opener(http_error=(401, '{"message":"Bad credentials"}'))
    with pytest.raises(AuthTokenError, match="HTTP 401"):
        mint_installation_token(
            "12345", pem, "9876", now=1_700_000_000, opener=opener
        )


def test_mint_surfaces_network_error(rsa_keypair):
    pem, _ = rsa_keypair
    opener = _fake_opener(url_error="connection refused")
    with pytest.raises(AuthTokenError, match="network failure"):
        mint_installation_token(
            "12345", pem, "9876", now=1_700_000_000, opener=opener
        )


def test_mint_rejects_malformed_json(rsa_keypair):
    pem, _ = rsa_keypair

    class _Opener:
        def open(self, request, timeout=None):  # noqa: A003
            class _R:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return None

                def read(self):
                    return b"not json"

            return _R()

    with pytest.raises(AuthTokenError, match="non-JSON"):
        mint_installation_token(
            "12345", pem, "9876", now=1_700_000_000, opener=_Opener()  # type: ignore[arg-type]
        )


def test_mint_rejects_response_missing_fields(rsa_keypair):
    pem, _ = rsa_keypair
    opener = _fake_opener({"token": "ghs_xxx"})  # missing expires_at
    with pytest.raises(AuthTokenError, match="missing token/expires_at"):
        mint_installation_token(
            "12345", pem, "9876", now=1_700_000_000, opener=opener
        )
