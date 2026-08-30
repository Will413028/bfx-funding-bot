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
  200 OK            — all LIVENESS_THRESHOLDS-listed sub-tasks have a
                      heartbeat within their threshold. Body = JSON
                      `{"status": "ok", "tasks": N}`.
                      Reactive activity sub-tasks (executor, safety_chain)
                      are intentionally excluded — idle trading must never
                      trigger a restart (k8s liveness anti-pattern).
  503 Service Unavailable — at least one liveness sub-task heartbeat is
                      stale OR no liveness sub-tasks are registered yet
                      (during startup). Body = JSON `{"stale": [...]}` or
                      `{"status": "starting", "reason": ...}` so log
                      scrapers can debug.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from bfx_funding_bot.modules.marketfeed.health_monitor import (
    LIVENESS_THRESHOLDS,
    HealthProbe,
)

if TYPE_CHECKING:
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner
    from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
    from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
    from bfx_funding_bot.modules.observability.metrics import DaemonMetrics

log = logging.getLogger(__name__)


def make_app(
    probe: HealthProbe,
    *,
    smoke_runner: SmokeRunner | None = None,
    admin_token: str | None = None,
    metrics: DaemonMetrics | None = None,
    trading_status: TradingStatusService | None = None,
    readiness: TradingReadiness | None = None,
) -> FastAPI:
    """Build the FastAPI app bound to a given HealthProbe instance.

    The admin router is mounted when `admin_token` is set AND at least one
    admin dependency is provided; each feature's routes then mount only if its
    own dependency is present (`smoke_runner` → POST /admin/smoke-test,
    `trading_status` → GET /admin/trading-status + POST /admin/dry-evaluate).
    With no token, no admin route is exposed at all — trading-status serves
    live balances and positions. Defaults preserve healthz-only behaviour.

    If `metrics` is provided, `GET /metrics` serves the Prometheus text
    exposition (Four Golden Signals — see observability/metrics.py). The
    handler is async on purpose: collectors read live daemon state (probe
    dicts, ws queue depth) and running on the event loop keeps those reads
    race-free with the single-threaded daemon mutations.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Liveness probe: ONLY own-loop, event-loop-driven sub-tasks count.
        # Reactive activity (executor / safety_chain) and unknown keys are
        # excluded — "no trading activity" must never trigger a restart
        # (k8s liveness anti-pattern). See health_monitor.LIVENESS_THRESHOLDS.
        now = datetime.now(UTC)
        liveness = {
            t: ts for t, ts in probe.last_active_ts.items()
            if t in LIVENESS_THRESHOLDS
        }
        if not liveness:
            return JSONResponse(
                status_code=503,
                content={"status": "starting", "reason": "no_liveness_sub_tasks_registered_yet"},
            )
        stale: list[dict[str, float | int | str]] = []
        for task, last_ts in liveness.items():
            threshold = LIVENESS_THRESHOLDS[task]
            age_s = (now - last_ts).total_seconds()
            if age_s > threshold:
                stale.append({"task": task, "age_s": age_s, "threshold_s": threshold})
        if stale:
            return JSONResponse(status_code=503, content={"status": "degraded", "stale": stale})
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "tasks": len(liveness)},
        )

    if readiness is not None:
        @app.get("/readyz")
        async def readyz() -> JSONResponse:
            snapshot = readiness.snapshot()
            return JSONResponse(
                status_code=200 if snapshot.trading_ready else 503,
                content={
                    "trading_ready": snapshot.trading_ready,
                    "reason": snapshot.reason,
                },
            )

    if metrics is not None:
        @app.get("/metrics")
        async def metrics_exposition() -> Response:
            return Response(content=metrics.render(), media_type=metrics.content_type)
        log.info("metrics_endpoint_mounted endpoint=/metrics")

    if admin_token and (smoke_runner is not None or trading_status is not None):
        from bfx_funding_bot.modules.admin.router import build_router
        app.include_router(build_router(
            smoke_runner=smoke_runner, admin_token=admin_token,
            trading_status=trading_status,
        ))
        log.info(
            "admin_router_mounted smoke_test=%s trading_status=%s",
            smoke_runner is not None, trading_status is not None,
        )
    else:
        log.info(
            "admin_router_skipped smoke_runner=%s trading_status=%s admin_token_set=%s",
            smoke_runner is not None, trading_status is not None, bool(admin_token),
        )

    return app


async def run_healthz_server(
    *,
    probe: HealthProbe,
    host: str,
    port: int,
    stop_event: asyncio.Event,
    smoke_runner: SmokeRunner | None = None,
    admin_token: str | None = None,
    metrics: DaemonMetrics | None = None,
    trading_status: TradingStatusService | None = None,
    readiness: TradingReadiness | None = None,
) -> None:
    """Run uvicorn until stop_event fires; cancellation safe.

    Bound to a uvicorn.Server so we can drive should_exit=True for
    graceful shutdown alongside the daemon's other tasks. Lifetime is
    tied to the daemon's TaskGroup — if uvicorn raises, the TaskGroup
    cancels all sibling tasks (same supervision contract as other
    sub-tasks per D4 spec).
    """
    app = make_app(
        probe, smoke_runner=smoke_runner, admin_token=admin_token, metrics=metrics,
        trading_status=trading_status, readiness=readiness,
    )
    config = uvicorn.Config(
        app=app, host=host, port=port,
        log_level="warning", access_log=False,
        loop="asyncio",
        # uvicorn default is None = wait forever for in-flight connections.
        # In paper-exit cycle this hung daemon TaskGroup drain indefinitely
        # (2026-05-21 finding) — bound it so cleanup is guaranteed to make progress.
        timeout_graceful_shutdown=5,
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
        # Outer timeout 10s = uvicorn's 5s graceful + 5s safety margin.
        # If uvicorn ignores both should_exit and cancel, daemon must still exit.
        try:
            await asyncio.wait_for(
                asyncio.gather(serve_task, stop_task, return_exceptions=True),
                timeout=10.0,
            )
        except TimeoutError:
            log.warning(
                "healthz_shutdown_timeout — uvicorn did not exit within 10s; "
                "daemon cleanup proceeding (serve_task may leak briefly)",
            )
        except (asyncio.CancelledError, Exception):
            pass
