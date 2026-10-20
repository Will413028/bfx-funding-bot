"""The web API's tailnet ingress for tests (9d-5): settings, and the headers Serve sends.

Serve forwards the operator capability the tailnet policy grants; tests send the same
header themselves. ``INGRESS_ENV`` is set for every test by ``tests/conftest.py``, so a
full web API lifespan starts; ``testserver`` (TestClient's Host) is an allowed host.
"""
from __future__ import annotations

import json

from fastapi import FastAPI

from bfx_funding_bot.core.auth import CAPABILITIES_HEADER, LOGIN_HEADER, OPERATOR_CAPABILITY
from bfx_funding_bot.core.settings import IngressSettings

OPERATOR_TAILNET_USER_ID = "operator-node-1"
ALLOWED_HOST = "bfx-ops.example.ts.net"
INGRESS_ENV = {
    "BFX_OPERATOR_TAILNET_USER_ID": OPERATOR_TAILNET_USER_ID,
    "BFX_WEBAPI_ALLOWED_HOSTS": f"{ALLOWED_HOST},testserver",
    "BFX_FE_SENTRY_DSN": "https://publickey@o1.ingest.sentry.io/42",
}


def ingress_settings(**overrides: str) -> IngressSettings:
    values = {**INGRESS_ENV, **overrides}
    return IngressSettings(**values)


def attach_ingress(app: FastAPI, **overrides: str) -> FastAPI:
    """What the lifespan does, for apps built from routers alone."""
    app.state.ingress = ingress_settings(**overrides)
    return app


def operator_headers(user_id: str = OPERATOR_TAILNET_USER_ID, login: str = "operator@github") -> dict[str, str]:
    """The headers Serve forwards for a caller the policy grants the operator capability."""
    return {
        CAPABILITIES_HEADER: json.dumps({OPERATOR_CAPABILITY: [{"user_id": user_id}]}),
        LOGIN_HEADER: login,
    }
