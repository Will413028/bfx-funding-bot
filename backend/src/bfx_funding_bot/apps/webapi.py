import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from bfx_funding_bot.apps.read_models import build_read_models
from bfx_funding_bot.core.authority import require_ledger_authority
from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.log_format import configure_logging
from bfx_funding_bot.core.schema_head import SchemaHeadMismatch, assert_schema_head
from bfx_funding_bot.core.security_log import BOOT_REFUSED, security_event
from bfx_funding_bot.core.settings import IngressSettings, Settings
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.attribution import build_attribution_router
from bfx_funding_bot.modules.api.config import build_config_router
from bfx_funding_bot.modules.api.deps import database_is_ready
from bfx_funding_bot.modules.api.funding_status import build_funding_status_router
from bfx_funding_bot.modules.api.ingress import install_ingress
from bfx_funding_bot.modules.api.projections import build_projections_router
from bfx_funding_bot.modules.api.public import build_public_router
from bfx_funding_bot.modules.api.routers import build_router as build_api_router
from bfx_funding_bot.modules.api.trading_control import build_trading_control_router
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router
from bfx_funding_bot.modules.observability.error_tracking import init_error_tracking


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if os.environ.get("BFX_ADMIN_TOKEN", "").strip():
        # The daemon's halt credential; the web API reads with BFX_ADMIN_READ_TOKEN only
        # (9d-2). Refusing to start makes a left-over key fail the deploy, which rolls back.
        security_event(BOOT_REFUSED, reason="admin_token_in_webapi_env", level=logging.CRITICAL)
        raise RuntimeError("bfx_admin_token_in_webapi_env")
    if "BFX_VAULT_KEK" in os.environ:
        # The vault key opens every exchange credential; only the bot decrypts (9g, F29).
        # Present at all, even empty, is a misplaced env file: refuse, as above.
        logging.critical("Startup refused: BFX_VAULT_KEK must not be in the web API's env")
        raise RuntimeError("bfx_vault_kek_in_webapi_env")
    # Outside the try below: a missing or invalid ingress value refuses to start, so the
    # deploy fails (and rolls back) instead of every request answering 503 (9d-5).
    try:
        app.state.ingress = IngressSettings()
    except Exception:
        security_event(BOOT_REFUSED, reason="ingress_settings_invalid", level=logging.CRITICAL)
        raise
    try:
        settings = Settings()
        configure_logging("webapi", level=settings.log_level)
        engine = make_engine(settings)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
    except Exception:
        logging.exception("Startup failed; /health remains available")
        app.state.engine = None
        app.state.session_factory = None
    factory = app.state.session_factory
    if factory is not None:
        # Read once, in the bot's order: a database at another schema than this image's
        # single migration head (ADR 2026-10-15 D6: the deploy stops the web API for a
        # migration, so another head means the wrong build for this database), or not on
        # the ledger (or an unreadable epoch), refuses to start, so /health never reports
        # a build on the wrong schema or authority and uvicorn exits.
        try:
            async with factory() as session:
                await assert_schema_head(session)
                await require_ledger_authority(session)
            app.state.read_models = build_read_models()
        except Exception as exc:
            if isinstance(exc, SchemaHeadMismatch):
                security_event(BOOT_REFUSED, reason="schema_head_mismatch", detail=str(exc),
                               level=logging.CRITICAL)
            else:
                security_event(BOOT_REFUSED, reason="capital_authority_unreadable",
                               level=logging.CRITICAL)
            await app.state.engine.dispose()
            raise
    try:
        yield
    finally:
        # Boot state belongs to this lifespan: a later run on the same app object
        # (tests, reload) must not inherit read models it did not build.
        app.state.read_models = None
        existing_engine = getattr(app.state, "engine", None)
        if existing_engine is not None:
            await existing_engine.dispose()


# At import: uvicorn has configured its own loggers by now and hands them to the root's
# JSON handler; a root someone else configured (pytest) is left alone. The lifespan sets
# the configured level.
configure_logging("webapi")
# Before the app exists, so the FastAPI integration sees every request.
init_error_tracking("webapi", os.environ,
                    integrations=(StarletteIntegration(), FastApiIntegration()))
# No generated docs: the API is the UI's, behind Serve (9d-5).
app = FastAPI(title="bfx-funding-bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(build_api_router())
app.include_router(build_api_keys_router())
app.include_router(build_attribution_router())
app.include_router(build_config_router())
app.include_router(build_funding_status_router())
app.include_router(build_projections_router())
app.include_router(build_public_router())
app.include_router(build_uncertainties_router())
app.include_router(build_trading_control_router())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    if await database_is_ready(request):
        return JSONResponse({"status": "ready", "checks": {"database": "ok"}})
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "not_ready", "checks": {"database": "failed"}},
    )


# Last: the static export is mounted at `/`, after every route (BFX_WEBAPI_STATIC_DIR is set
# by the image; tests and local runs without an export serve the API only).
install_ingress(app, static_dir=Path(os.environ.get("BFX_WEBAPI_STATIC_DIR", "/app/static")))
