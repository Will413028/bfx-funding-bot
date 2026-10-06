import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from bfx_funding_bot.apps.authority_support import SUPPORTED
from bfx_funding_bot.apps.read_models import select_read_models
from bfx_funding_bot.core.authority import read_authority
from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.api.api_keys import build_api_keys_router
from bfx_funding_bot.modules.api.attribution import build_attribution_router
from bfx_funding_bot.modules.api.config import build_config_router
from bfx_funding_bot.modules.api.deps import database_is_ready
from bfx_funding_bot.modules.api.funding_status import build_funding_status_router
from bfx_funding_bot.modules.api.projections import build_projections_router
from bfx_funding_bot.modules.api.public import build_public_router
from bfx_funding_bot.modules.api.routers import build_router as build_api_router
from bfx_funding_bot.modules.api.trading_control import build_trading_control_router
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        settings = Settings()
        logging.basicConfig(level=settings.log_level)
        engine = make_engine(settings)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
    except Exception:
        logging.exception("Startup failed; /health remains available")
        app.state.engine = None
        app.state.session_factory = None
    factory = app.state.session_factory
    if factory is not None:
        # Read once: a database not on the ledger (or an unreadable epoch) refuses to
        # start, so /health never reports a build on the wrong authority.
        try:
            async with factory() as session:
                authority = await read_authority(session, supported=SUPPORTED)
            app.state.authority = authority
            app.state.read_models = select_read_models()
        except Exception:
            logging.critical("Startup refused: capital authority unreadable or unsupported")
            await app.state.engine.dispose()
            raise
    try:
        yield
    finally:
        # Boot state belongs to this lifespan: a later run on the same app object
        # (tests, reload) must not inherit an authority it did not read.
        app.state.authority = None
        app.state.read_models = None
        existing_engine = getattr(app.state, "engine", None)
        if existing_engine is not None:
            await existing_engine.dispose()


app = FastAPI(title="bfx-funding-bot", lifespan=lifespan)
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
