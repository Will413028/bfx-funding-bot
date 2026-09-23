import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from bfx_funding_bot.core import auth as auth_mod


def _mint(claims: dict, key: Ed25519PrivateKey, kid: str = "test-kid") -> str:
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": kid})


@pytest.fixture()
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    class _FakeJWKClient:
        def __init__(self, *a, **k): ...
        def get_signing_key_from_jwt(self, _token):
            class _K:  # PyJWT signing-key shape
                key = pub
            return _K()

    monkeypatch.setattr(auth_mod, "_jwk_client", _FakeJWKClient())
    return priv


def _claims(**over):
    now = dt.datetime.now(tz=dt.UTC)
    base = {
        "sub": "user_123", "email": "will@example.com", "role": "admin",
        "iss": "https://app.example.com", "aud": "bfx-funding-backend",
        "iat": now, "exp": now + dt.timedelta(minutes=15),
    }
    base.update(over)
    return base


def test_valid_token_returns_principal(keypair, monkeypatch):
    monkeypatch.setattr(auth_mod, "_ISSUER", "https://app.example.com")
    token = _mint(_claims(), keypair)
    principal = auth_mod._verify(token)
    assert principal.user_id == "user_123"
    assert principal.email == "will@example.com"
    assert principal.role == "admin"


def test_expired_token_raises_401(keypair, monkeypatch):
    monkeypatch.setattr(auth_mod, "_ISSUER", "https://app.example.com")
    now = dt.datetime.now(tz=dt.UTC)
    token = _mint(_claims(exp=now - dt.timedelta(minutes=1)), keypair)
    with pytest.raises(HTTPException) as ei:
        auth_mod._verify(token)
    assert ei.value.status_code == 401


def test_wrong_audience_raises_401(keypair, monkeypatch):
    monkeypatch.setattr(auth_mod, "_ISSUER", "https://app.example.com")
    token = _mint(_claims(aud="someone-else"), keypair)
    with pytest.raises(HTTPException) as ei:
        auth_mod._verify(token)
    assert ei.value.status_code == 401


def test_wrong_issuer_raises_401(keypair, monkeypatch):
    monkeypatch.setattr(auth_mod, "_ISSUER", "bfx-funding-bot")
    token = _mint(_claims(iss="someone-else"), keypair)
    with pytest.raises(HTTPException) as ei:
        auth_mod._verify(token)
    assert ei.value.status_code == 401


def test_pinned_issuer_contract(keypair, monkeypatch):
    """Cross-system contract: the FE jwt plugin pins `issuer: "bfx-funding-bot"`
    (frontend/src/lib/auth.ts), decoupled from the deploy URL, and the BE default +
    verifier must agree so a domain change can't break verification. No env config
    needed for issuer at deploy (only the real JWKS URL must be set)."""
    # Robust against ambient env: assert the wired-in DEFAULT, not whatever a shell set.
    monkeypatch.delenv("BETTER_AUTH_ISSUER", raising=False)
    from bfx_funding_bot.core.settings import AuthSettings

    assert AuthSettings().better_auth_issuer == "bfx-funding-bot"  # BE default == FE pin
    # A FE-shaped token (iss == the pin) verifies when the verifier uses the pin.
    monkeypatch.setattr(auth_mod, "_ISSUER", "bfx-funding-bot")
    token = _mint(_claims(iss="bfx-funding-bot"), keypair)
    principal = auth_mod._verify(token)
    assert principal.user_id == "user_123"
    assert principal.role == "admin"
