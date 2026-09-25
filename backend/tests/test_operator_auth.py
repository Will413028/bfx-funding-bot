import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from bfx_funding_bot.core import auth
from bfx_funding_bot.core.auth import Principal
from bfx_funding_bot.core.settings import AuthSettings


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_require_operator_accepts_only_configured_admin(monkeypatch):
    """Rejects the regression that admits a matching user without admin role."""
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")
    monkeypatch.setenv("BFX_OPERATOR_ROLE", "admin")
    monkeypatch.setattr(auth, "_verify", lambda _: Principal("operator-1", None, "admin"))

    principal = asyncio.run(auth.require_operator(_credentials("token")))

    assert principal.user_id == "operator-1"


@pytest.mark.parametrize(
    ("principal", "detail"),
    [
        (Principal("other", None, "admin"), "operator_required"),
        (Principal("operator-1", None, "user"), "operator_role_required"),
    ],
)
def test_require_operator_rejects_unconfigured_principal(monkeypatch, principal, detail):
    """Rejects the regression that authorizes another user or a non-admin role."""
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")
    monkeypatch.setattr(auth, "_verify", lambda _: principal)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_operator(_credentials("token")))

    assert exc.value.status_code == 403
    assert exc.value.detail == detail


def test_missing_operator_id_fails_closed(monkeypatch):
    """Rejects the regression that falls back to an arbitrary authenticated user."""
    monkeypatch.delenv("BFX_OPERATOR_USER_ID", raising=False)
    monkeypatch.setattr(auth, "_verify", lambda _: Principal("operator-1", None, "admin"))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_operator(_credentials("token")))

    assert exc.value.status_code == 503
    assert exc.value.detail == "auth_not_configured"


def test_require_operator_verifies_token_before_checking_configuration(monkeypatch):
    """Rejects the regression that conceals invalid credentials as a config error."""
    monkeypatch.delenv("BFX_OPERATOR_USER_ID", raising=False)

    def raise_invalid_token(_token: str) -> Principal:
        raise HTTPException(status_code=401, detail="invalid_token")

    monkeypatch.setattr(auth, "_verify", raise_invalid_token)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_operator(_credentials("invalid")))

    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid_token"


@pytest.mark.parametrize("phase", ["live"])
def test_production_phase_rejects_non_admin_operator_role(monkeypatch, phase):
    """Rejects the regression that allows a non-admin production role setting."""
    monkeypatch.setenv("BFX_PHASE", phase)
    monkeypatch.setenv("BFX_OPERATOR_ROLE", "user")

    with pytest.raises(ValueError, match="BFX_OPERATOR_ROLE must be 'admin'"):
        AuthSettings()


def test_auth_settings_does_not_require_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")

    settings = AuthSettings()

    assert settings.operator_user_id == "operator-1"
    assert settings.operator_role == "admin"


def test_main_import_is_db_independent() -> None:
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.pop("BFX_OPERATOR_USER_ID", None)
    environment.pop("BETTER_AUTH_JWKS_URL", None)
    backend_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "from bfx_funding_bot.main import app; print(app.title)"],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bfx-funding-bot"
