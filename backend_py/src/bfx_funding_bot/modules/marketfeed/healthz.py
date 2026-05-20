"""External HTTP liveness endpoint — `GET /healthz` for Koyeb / k8s probe.

Phase 4.2.1+ best-practice item: daemon-internal HealthMonitor.scan_staleness
can detect a stalled sub-task, but cannot detect a daemon-wide deadlock
(asyncio event loop blocked → scan_staleness itself never runs). An external
HTTP probe escapes that failure mode — the platform's health checker
sees no response and restarts the container.

This module exposes a small FastAPI app and a `run_healthz_server`
coroutine; the daemon adds it as one task in its TaskGroup. Reads from the
shared HealthProbe — no new state.

Responses:
  200 OK            — all SUB_TASK_THRESHOLDS-listed sub-tasks have a
                      heartbeat within their threshold. Body = JSON
                      `{"status": "ok", "tasks": N}`.
  503 Service Unavailable — at least one sub-task heartbeat is stale OR
                      no sub-tasks are registered yet (during startup).
                      Body = JSON `{"stale": [...]}` listing per-task age
                      vs threshold so log scrapers can debug.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bfx_funding_bot.modules.marketfeed.health_monitor import (
    _DEFAULT_THRESHOLD_S,
    SUB_TASK_THRESHOLDS,
    HealthProbe,
)

log = logging.getLogger(__name__)


def make_app(probe: HealthProbe) -> FastAPI:
    """Build the FastAPI app bound to a given HealthProbe instance."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        last_active = dict(probe.last_active_ts)  # snapshot
        if not last_active:
            return JSONResponse(
                status_code=503,
                content={"status": "starting", "reason": "no_sub_tasks_registered_yet"},
            )
        now = datetime.now(UTC)
        stale: list[dict[str, float | int | str]] = []
        for task, last_ts in last_active.items():
            threshold = SUB_TASK_THRESHOLDS.get(task, _DEFAULT_THRESHOLD_S)
            age_s = (now - last_ts).total_seconds()
            if age_s > threshold:
                stale.append({"task": task, "age_s": age_s, "threshold_s": threshold})
        if stale:
            return JSONResponse(status_code=503, content={"status": "degraded", "stale": stale})
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "tasks": len(last_active)},
        )

    return app


async def run_healthz_server(
    *,
    probe: HealthProbe,
    host: str,
    port: int,
    stop_event: asyncio.Event,
) -> None:
    """Run uvicorn until stop_event fires; cancellation safe.

    Bound to a uvicorn.Server so we can drive should_exit=True for
    graceful shutdown alongside the daemon's other tasks. Lifetime is
    tied to the daemon's TaskGroup — if uvicorn raises, the TaskGroup
    cancels all sibling tasks (same supervision contract as other
    sub-tasks per D4 spec).
    """
    app = make_app(probe)
    config = uvicorn.Config(
        app=app, host=host, port=port,
        log_level="warning", access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve(), name="healthz_uvicorn")
    stop_task = asyncio.create_task(stop_event.wait(), name="healthz_stop_wait")
    try:
        await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        server.should_exit = True
        for t in (serve_task, stop_task):
            if not t.done():
                t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(serve_task, stop_task, return_exceptions=True)
