"""Every private webapi route admits only the operator; the public surface is a fixed list.

Walks each mounted ``APIRoute``'s effective dependency tree (router-level ``dependencies=``
included) through ``iter_route_contexts``: since FastAPI 0.142 ``include_router`` mounts a
lazy ``_IncludedRouter``, so ``app.routes`` alone no longer lists the API routes. A route
added without ``require_operator`` anywhere in its chain fails here by name.

Mutation checks: drop ``require_operator`` from a router's dependency chain, or mount an
unguarded ``/api/v1/...`` route — both fail ``test_private_routes_require_the_operator``.
"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts

from bfx_funding_bot.apps.webapi import app
from bfx_funding_bot.core.auth import require_operator
from bfx_funding_bot.modules.api.ingress import FE_EVENTS_PATH

PUBLIC_API_PATHS = frozenset({
    "/api/v1/public/proof-summary",
    "/api/v1/public/funding-rates.csv",
})
PROBE_PATHS = frozenset({"/health", "/ready"})
# The ingress's own routes (9d-5): `/` redirects to the UI, /fe-events forwards browser
# Sentry events; both are guarded by the ingress middleware's same-origin check, not auth.
INGRESS_PATHS = frozenset({"/", FE_EVENTS_PATH})
# The static export's mount (present only where the image ships it); generated docs are off.
UI_MOUNT = "/"
DOCS_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
_AUTH_DEPENDENCIES = (require_operator,)


def _calls(dependant: Dependant) -> Iterator[object]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _api_routes(target: FastAPI) -> list[RouteContext]:
    return [
        context for context in iter_route_contexts(target.routes)
        if isinstance(context.original_route, APIRoute)
    ]


def _unguarded_private_routes(target: FastAPI) -> list[str]:
    return sorted(
        f"{sorted(route.methods)} {route.path}"
        for route in _api_routes(target)
        if route.path not in PUBLIC_API_PATHS | PROBE_PATHS | INGRESS_PATHS
        and require_operator not in set(_calls(route.dependant))
    )


def test_private_routes_require_the_operator() -> None:
    assert _unguarded_private_routes(app) == []


def test_every_route_is_versioned_api_a_probe_or_the_ingress() -> None:
    """A mount, websocket or plain Starlette route would bypass the APIRoute walk above; the
    only one allowed is the UI's static mount, and there are no generated docs."""
    other = sorted(
        str(context.path) for context in iter_route_contexts(app.routes)
        if not isinstance(context.original_route, APIRoute)
    )
    assert set(other) <= {UI_MOUNT}
    assert not set(other) & DOCS_PATHS
    stray = sorted(
        route.path for route in _api_routes(app)
        if not route.path.startswith("/api/v1/") and route.path not in PROBE_PATHS | INGRESS_PATHS
    )
    assert stray == []
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None


def test_public_routes_exist_and_carry_no_auth() -> None:
    by_path = {route.path: route for route in _api_routes(app)}
    assert by_path.keys() >= PUBLIC_API_PATHS | PROBE_PATHS | INGRESS_PATHS
    authed = sorted(
        path for path in PUBLIC_API_PATHS | PROBE_PATHS | INGRESS_PATHS
        if set(_calls(by_path[path].dependant)) & set(_AUTH_DEPENDENCIES)
    )
    assert authed == []


def test_the_walk_catches_an_unguarded_route() -> None:
    """The checker itself: a fresh app with one guarded and one bare route."""
    from fastapi import Depends

    probe = FastAPI()

    @probe.get("/api/v1/guarded", dependencies=[Depends(require_operator)])
    async def guarded() -> None:
        return None

    @probe.get("/api/v1/x")
    async def bare() -> None:
        return None

    assert _unguarded_private_routes(probe) == ["['GET'] /api/v1/x"]
