import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from bfx_funding_bot.core import auth
from bfx_funding_bot.core.settings import Settings
from tests.ingress_support import (
    INGRESS_ENV,
    OPERATOR_TAILNET_USER_ID,
    ingress_settings,
    operator_headers,
)


def _request(headers: dict[str, str], *, ingress: bool = True) -> Request:
    app = FastAPI()
    if ingress:
        app.state.ingress = ingress_settings()
    scope = {
        "type": "http", "method": "GET", "path": "/api/v1/profile", "app": app,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_require_operator_accepts_the_capability_for_the_configured_user():
    principal = asyncio.run(auth.require_operator(_request(operator_headers(login="will@github"))))

    assert principal.user_id == OPERATOR_TAILNET_USER_ID
    assert principal.email == "will@github"


@pytest.mark.parametrize("headers", [
    pytest.param({}, id="no-capability-header"),
    pytest.param({auth.CAPABILITIES_HEADER: "not json"}, id="malformed"),
    pytest.param({auth.CAPABILITIES_HEADER: json.dumps({"example.com/cap/other": [{"user_id": OPERATOR_TAILNET_USER_ID}]})},
                 id="another-capability"),
    pytest.param({auth.CAPABILITIES_HEADER: json.dumps({auth.OPERATOR_CAPABILITY: [{"role": "operator"}]})},
                 id="no-user-id"),
    pytest.param(operator_headers(user_id="someone-else"), id="another-user"),
])
def test_require_operator_refuses_anything_but_the_operator_capability(headers):
    """Serve forwards only what the tailnet policy grants; anything else is no operator."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_operator(_request(headers)))

    assert exc.value.status_code == 401


def test_missing_ingress_settings_fail_closed():
    """An app built without the lifespan's settings refuses rather than admitting anyone."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.require_operator(_request(operator_headers(), ingress=False)))

    assert exc.value.status_code == 503
    assert exc.value.detail == "auth_not_configured"


def test_settings_never_require_the_ingress_values(monkeypatch):
    """IngressSettings stays off the Settings chain: other processes build Settings without
    the web API's keys."""
    for key in INGRESS_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")

    assert Settings().database_url.startswith("postgresql://")


def test_main_import_is_db_independent() -> None:
    """Importing the web API needs neither a database nor the ingress values: they are read
    in the lifespan only."""
    environment = os.environ.copy()
    for key in ("DATABASE_URL", *INGRESS_ENV):
        environment.pop(key, None)
    backend_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "from bfx_funding_bot.apps.webapi import app; print(app.title)"],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bfx-funding-bot"


@pytest.mark.parametrize("missing", sorted(INGRESS_ENV))
def test_the_web_api_refuses_to_start_without_each_ingress_value(monkeypatch, missing):
    """Required at every start, never gated on BFX_PHASE (the web API reads no live.env)."""
    from bfx_funding_bot.apps.webapi import app

    monkeypatch.delenv("BFX_PHASE", raising=False)
    monkeypatch.delenv(missing)

    with pytest.raises(Exception, match=missing), TestClient(app):
        pass
