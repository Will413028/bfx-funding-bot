"""Web-API auth: the operator identity Tailscale Serve forwards (9d-5, Q1-E).

The web API listens on 127.0.0.1 inside the tailnet node's network namespace, so only
Serve reaches it. Serve replaces any client-sent ``Tailscale-*`` header with the real
values (9d-0 spike) and forwards the app capability the tailnet policy grants the caller
(``AcceptAppCaps`` in serve.json). The authorization is that capability: a request without
it, or whose capability names another tailnet user, is refused. The principal's key is the
operator's tailnet node user ID, the ``user_id`` of their exchange-account memberships.
"""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from bfx_funding_bot.core.settings import IngressSettings

# The capability the tailnet policy grants the operator on tag:bfx-ops; its value carries
# the operator's node user ID: {"github.com/...": [{"user_id": "<id>"}]}.
OPERATOR_CAPABILITY = "github.com/Will413028/bfx-funding-bot/cap/operator"
CAPABILITIES_HEADER = "Tailscale-App-Capabilities"
LOGIN_HEADER = "Tailscale-User-Login"


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    role: str


def ingress_settings(request: Request) -> IngressSettings:
    """The lifespan's IngressSettings; 503 when the app was built without it (fail closed)."""
    settings = getattr(request.app.state, "ingress", None)
    if not isinstance(settings, IngressSettings):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth_not_configured")
    return settings


def _capability_user_ids(raw: str) -> list[str]:
    try:
        capabilities = json.loads(raw)
    except ValueError:
        return []
    values = capabilities.get(OPERATOR_CAPABILITY) if isinstance(capabilities, dict) else None
    if not isinstance(values, list):
        return []
    return [str(value["user_id"]) for value in values
            if isinstance(value, dict) and isinstance(value.get("user_id"), str | int)]


async def require_operator(request: Request) -> Principal:
    """FastAPI dependency -- the authz boundary for every private /api/v1 endpoint."""
    settings = ingress_settings(request)
    raw = request.headers.get(CAPABILITIES_HEADER)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="operator_identity_missing")
    expected = settings.operator_tailnet_user_id
    if not any(hmac.compare_digest(user_id.encode(), expected.encode())
               for user_id in _capability_user_ids(raw)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="operator_required")
    return Principal(user_id=expected, email=request.headers.get(LOGIN_HEADER), role="operator")
