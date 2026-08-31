"""SP1 web-API auth: verify Better Auth EdDSA JWTs via JWKS.

The standalone FastAPI web-API authorizes its /api/v1 endpoints by verifying
the short-lived JWT the Next BFF proxy forwards. Stateless: no DB hit, no call
back to the auth server beyond the cached JWKS fetch.
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from bfx_funding_bot.core.settings import AuthSettings

_settings = AuthSettings()
_ISSUER = _settings.better_auth_issuer
_AUDIENCE = _settings.jwt_audience
# Module-level client: caches JWKS keys by kid, refetches on unknown kid.
_jwk_client = PyJWKClient(_settings.better_auth_jwks_url, cache_keys=True) if _settings.better_auth_jwks_url else None

_bearer = HTTPBearer(auto_error=True)


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str | None
    role: str


def _verify(token: str) -> Principal:
    if _jwk_client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth_not_configured")
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["EdDSA"],
            issuer=_ISSUER,
            audience=_AUDIENCE,
            leeway=10,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token") from e
    return Principal(user_id=claims["sub"], email=claims.get("email"), role=claims.get("role", "user"))


async def require_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),  # noqa: B008  (FastAPI DI idiom)
) -> Principal:
    """FastAPI dependency — the authz boundary for all /api/v1 endpoints."""
    return _verify(creds.credentials)


async def require_operator(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),  # noqa: B008  (FastAPI DI idiom)
) -> Principal:
    """FastAPI dependency that admits only the configured admin operator."""
    principal = _verify(creds.credentials)
    settings = AuthSettings()
    if not settings.operator_user_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth_not_configured")
    if principal.user_id != settings.operator_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator_required")
    if principal.role != settings.operator_role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="operator_role_required")
    return principal
