import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.api.routers import build_router as build_api_router


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
    try:
        yield
    finally:
        existing_engine = getattr(app.state, "engine", None)
        if existing_engine is not None:
            await existing_engine.dispose()


app = FastAPI(title="bfx-funding-bot", lifespan=lifespan)
app.include_router(build_api_router())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
