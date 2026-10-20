"""The web API's tailnet ingress (9d-5): request checks, the UI's static files, /fe-events.

Serve is the only way in (the web API listens on 127.0.0.1 in the tailnet node's network
namespace), so these checks are about the browser, not the network:

- ``Host`` must be one of ``BFX_WEBAPI_ALLOWED_HOSTS`` (DNS rebinding); the probes on
  /health and /ready, which bfx-deploy sends to 127.0.0.1 from inside the container, are
  exempt.
- A state-changing request must come from one of those origins (``Origin`` required and
  equal, ``Sec-Fetch-Site`` same-origin when sent) and, when it carries a body (except
  /fe-events), carry JSON, so a cross-site form or ``no-cors`` fetch cannot reach a
  handler. A bodiless DELETE needs no content type: the Origin check already stops it.
- The static export of the frontend is served with a hash-based CSP: the hashes of every
  inline script in the export, computed once at start.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect
from starlette.routing import Match

from bfx_funding_bot.core.settings import IngressSettings

PROBE_PATHS = frozenset({"/health", "/ready"})
FE_EVENTS_PATH = "/fe-events"
API_FALLBACK_PATH = "/api/{rest:path}"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SAME_SITE_FETCH = frozenset({"same-origin", "none"})
HOME = "/en/overview/"
MAX_EVENT_BYTES = 1024 * 1024
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def content_security_policy(script_hashes: frozenset[str]) -> str:
    """The static UI's policy: own scripts plus exactly the export's inline ones."""
    hashes = "".join(f" 'sha256-{digest}'" for digest in sorted(script_hashes))
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self'{hashes}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ])


def inline_script_hashes(directory: Path) -> frozenset[str]:
    """sha256 (base64) of every inline <script> body in the export's HTML files."""
    digests = set()
    for page in directory.rglob("*.html"):
        for body in _INLINE_SCRIPT.findall(page.read_bytes()):
            if body.strip():
                digests.add(base64.b64encode(hashlib.sha256(body).digest()).decode())
    return frozenset(digests)


def _refuse(status: int, detail: str) -> Response:
    return JSONResponse({"detail": detail}, status_code=status)


class IngressMiddleware:
    """Host allowlist and same-origin JSON for state changes; security headers on every
    response (and the CSP on the UI's). Outermost, so no route answers an unchecked request."""

    def __init__(self, app: ASGIApp, *, csp: Callable[[], str | None]) -> None:
        self.app = app
        self.csp = csp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if path not in PROBE_PATHS:
            refusal = self._check(request)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        policy = None if path.startswith("/api/") or path == FE_EVENTS_PATH else self.csp()

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                if policy is not None:
                    headers.append((b"content-security-policy", policy.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _check(self, request: Request) -> Response | None:
        settings = getattr(request.app.state, "ingress", None)
        if not isinstance(settings, IngressSettings):
            return _refuse(503, "ingress_not_configured")
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip().lower()
        if host not in settings.allowed_hosts:
            return _refuse(400, "host_not_allowed")
        if request.method in SAFE_METHODS:
            return None
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site is not None and fetch_site.lower() not in SAME_SITE_FETCH:
            return _refuse(403, "cross_site_refused")
        origin = request.headers.get("origin")
        if origin is None or origin.lower() not in {f"https://{h}" for h in settings.allowed_hosts}:
            return _refuse(403, "cross_site_refused")
        if request.url.path != FE_EVENTS_PATH and _has_body(request):
            media = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if media != "application/json":
                return _refuse(415, "json_required")
        return None


def _has_body(request: Request) -> bool:
    length = request.headers.get("content-length")
    return "transfer-encoding" in request.headers or (length is not None and length.strip() != "0")


def _dsn_parts(dsn: str) -> tuple[str, str | None, str, str, str] | None:
    """(host, port, path prefix, project id, public key) of a Sentry DSN, or None."""
    parts = urlsplit(dsn)
    prefix, _, project = parts.path.rstrip("/").rpartition("/")
    if not parts.hostname or not project or not parts.username:
        return None
    port = str(parts.port) if parts.port is not None else None
    return parts.hostname, port, prefix, project, parts.username


def sentry_envelope_target(envelope: bytes, dsn: str) -> str | None:
    """The configured project's envelope endpoint when the envelope's header names that
    project's DSN; None for anything else (another project, host, port or key, no header).
    The target is built from the configured DSN only, never from the envelope."""
    header = envelope.split(b"\n", 1)[0]
    try:
        named = _dsn_parts(str(json.loads(header).get("dsn", "")))
    except (ValueError, AttributeError):
        return None
    allowed = _dsn_parts(dsn)
    if allowed is None or named != allowed:
        return None
    host, port, prefix, project, _key = allowed
    netloc = host if port is None else f"{host}:{port}"
    return f"https://{netloc}{prefix}/api/{project}/envelope/"


async def _read_limited(request: Request, limit: int) -> bytes | None:
    """The body, or None as soon as it exceeds `limit` (never read whole first)."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def install_ingress(app: FastAPI, *, static_dir: Path) -> None:
    """The ingress middleware, /fe-events, `/` and the static export (when present)."""
    state: dict[str, str | None] = {"csp": None}
    app.add_middleware(IngressMiddleware, csp=lambda: state["csp"])

    @app.post(FE_EVENTS_PATH, include_in_schema=False)
    async def fe_events(request: Request) -> Response:
        """Browser Sentry events, forwarded only to the configured frontend project."""
        try:
            body = await _read_limited(request, MAX_EVENT_BYTES)
        except ClientDisconnect:
            return _refuse(400, "client_disconnected")
        if body is None:
            return _refuse(413, "event_too_large")
        settings: IngressSettings = request.app.state.ingress
        target = sentry_envelope_target(body, settings.fe_sentry_dsn)
        if target is None:
            return _refuse(400, "dsn_not_allowed")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                forwarded = await client.post(target, content=body,
                                              headers={"Content-Type": "application/x-sentry-envelope"})
        except httpx.HTTPError:
            return _refuse(502, "sentry_unreachable")
        return Response(status_code=forwarded.status_code)

    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse(HOME, status_code=307)

    # Before the static mount: an unknown API path answers JSON, not the export's 404 page.
    @app.api_route(API_FALLBACK_PATH, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                   include_in_schema=False)
    async def no_such_api(request: Request, rest: str) -> Response:
        # A real route on this path with another method: 405, as Starlette would answer.
        if any(getattr(route, "path", None) != API_FALLBACK_PATH
               and route.matches(request.scope)[0] is Match.PARTIAL
               for route in request.app.router.routes):
            return _refuse(405, "method_not_allowed")
        return _refuse(404, "not_found")

    if not static_dir.is_dir():
        logging.warning("No frontend export at %s; the web API serves the API only", static_dir)
        return
    state["csp"] = content_security_policy(inline_script_hashes(static_dir))
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
