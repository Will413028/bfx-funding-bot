"""build_daemon wires the SAME deployment_env into the emit + PG-store paths.

Phase 4.4c: AxiomReplayQueryAdapter removed from boot path (replaced by PG
from_snapshot). The emit-env invariant is now checked via StdoutEventSink:
  StdoutEventSink._resource.deployment_environment.value == BFX_DEPLOYMENT_ENV

Phase 3c T10: AxiomClient removed; invariant migrated to stdout_sink path,
accessed via daemon.monitor._events._resource (StdoutEventSink injected into
HealthMonitor which is a Daemon field).
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest_httpx import HTTPXMock


def _write_cells_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "cells.yaml"
    # fUST: the funded canary currency (caps {fUSD: 0, fUST: 3000}). Several
    # tests here build a canary daemon, which now boot-asserts cap > 0 per
    # configured symbol (assert_caps_invariant), so fUSD (cap 0) cannot be used.
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUST
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    return yaml_path


# Valid (phase, realm) deployable combos — the config.py phase<->realm guard
# rejects canary+shadow and simulated+prod, so pair each realm value with a
# phase that can actually ship it. Still exercises all 3 realm values flowing
# through to the emit sink (the invariant under test).
@pytest.mark.parametrize(
    "phase,env_value",
    [("paper", "ci"), ("shadow", "shadow"), ("canary", "prod")],
)
@pytest.mark.asyncio
async def test_build_daemon_emit_and_query_env_symmetric(
    phase: str,
    env_value: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.setenv("BFX_PHASE", phase)
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", env_value)
    monkeypatch.setenv(
        "BFX_EXECUTION_POLICY",
        "paper" if phase == "paper" else "book_guarded",
    )
    if phase != "paper":
        monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
        monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
        monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    if phase == "canary":
        # canary boot requires all safety guards enabled (assert_canary_guard_invariant).
        # Executor stays paper (BFX_EXECUTOR unset) — fine for an env-wiring unit test.
        safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
        monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")  # avoid git subprocess
    # Phase 4.4c: file-based sqlite so event-store tables created below are
    # visible to build_daemon's engine (from_snapshot uses them at boot).
    db_path = tmp_path / "daemon_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    # warmup_cell fetches Bitfinex candles with file-based sqlite.
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET",
        status_code=200,
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path),
        skip_ws=True,
    )

    # Invariant: StdoutEventSink (the live emit path) must be wired with the
    # EventResource that carries BFX_DEPLOYMENT_ENV. Accessed via the
    # HealthMonitor's injected event_sink (both monitor + signal_engine share
    # the same stdout_sink instance, so one check suffices).
    # Phase 4.4c: PG-store env correctness covered by unit tests on build_daemon.
    from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink

    sink = daemon.monitor._events
    assert isinstance(sink, StdoutEventSink)
    assert sink._resource.deployment_environment.value == env_value


@pytest.mark.asyncio
async def test_build_daemon_reconcile_interval_zero_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """BFX_RECONCILE_INTERVAL_S <= 0 must raise ValueError at boot (canary phase,
    live block) to prevent a busy-loop hammering Bitfinex REST."""
    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "reconcile_guard.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.setenv("BFX_RECONCILE_INTERVAL_S", "0")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    # Must use the live executor path to enter the `if not spec.is_simulated` block
    # where the guard lives; paper executor sets is_simulated=True and skips it.
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET",
        status_code=200,
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    yaml_path = tmp_path / "cells.yaml"
    # fUST (funded canary currency) so build passes assert_caps_invariant and
    # reaches the reconcile-interval guard under test.
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUST
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")

    with pytest.raises(ValueError, match="BFX_RECONCILE_INTERVAL_S must be > 0"):
        await build_daemon(cells_yaml_path=yaml_path, skip_ws=True)


@pytest.mark.asyncio
async def test_auth_ws_resync_wired_to_periodic_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Live executor + WS client: auth_ws.on_resync_needed is bound to
    periodic_reconcile.request_resync so a stream break triggers an off-interval
    reconcile."""
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "resync_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.setenv("BFX_RESYNC_MIN_INTERVAL_S", "7")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET",
        status_code=200,
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)

    assert daemon.auth_ws is not None
    assert daemon.periodic_reconcile is not None
    # bound method equality: same __self__ + __func__
    assert daemon.auth_ws._on_resync_needed == daemon.periodic_reconcile.request_resync
    assert daemon.periodic_reconcile._min_resync_interval_s == 7.0
    # Shared-nonce wiring invariant (2026-07 auth-WS flap fix): every auth client
    # on the one BFX_API_KEY MUST draw from ONE monotonic nonce source. A separate
    # provider at a smaller scale (the old ms WS default vs µs REST) gets rejected
    # "nonce: small" and that client can never authenticate — regression guard.
    assert daemon.auth_ws._nonce_provider is daemon.executor._nonce_provider  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_live_boot_wires_one_book_service_readiness_and_audited_deployment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Live boot re-enables deployment only with the concrete integrity set."""
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "optimizer_live")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_FILL_MODEL_ARTIFACT", "models/fUST-fill.json")
    monkeypatch.setenv("BFX_OPTIMIZER_FEE_RATE", "0.15")
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "integrity_bootstrap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET",
        status_code=200,
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)

    from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
    from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookService
    from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness

    assert isinstance(daemon.trading_readiness, TradingReadiness)
    assert isinstance(daemon.funding_book_service, FundingBookService)
    assert daemon.periodic_reconcile is not None
    assert isinstance(daemon.periodic_reconcile._deployment, DeploymentReconciler)
    deployment = daemon.periodic_reconcile._deployment
    assert deployment._book_provider is daemon.funding_book_service
    assert deployment._execution_gate._readiness is daemon.trading_readiness
    assert deployment._optimizer_fee_rate == Decimal("0.15")


@pytest.mark.asyncio
async def test_daemon_taskgroup_runs_book_service_through_its_finally_shutdown() -> None:
    """Daemon owns TaskGroup supervision; service.run owns exactly-one stop."""
    from bfx_funding_bot.modules.marketfeed.daemon import Daemon

    class _BookService:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.stops = 0

        async def run(self, stop_event: asyncio.Event) -> None:
            try:
                self.started.set()
                await stop_event.wait()
            finally:
                self.stops += 1

    async def wait_for_stop() -> None:
        await daemon._stop_event.wait()

    daemon = object.__new__(Daemon)
    daemon.config = SimpleNamespace(cells=[])
    daemon.boot_recovery = None
    daemon.writer_lock = None
    daemon.ws_client = None
    daemon.fill_tracker = None
    daemon.ws_dispatcher = None
    daemon.book_snapshot_writer = None
    daemon.auth_ws = None
    daemon.periodic_reconcile = None
    daemon._stop_event = asyncio.Event()
    daemon._candle_writer_loop = wait_for_stop
    daemon._scheduler_loop = wait_for_stop
    daemon._monitor_loop = wait_for_stop
    daemon._heartbeat_scan_loop = wait_for_stop
    daemon._db_keepalive_loop = wait_for_stop
    daemon._healthz_server_loop = wait_for_stop
    service = _BookService()
    daemon.funding_book_service = service

    task = asyncio.create_task(daemon.run())
    await service.started.wait()
    daemon._stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert service.stops == 1


@pytest.mark.asyncio
async def test_smoke_runner_present_for_simulated_paper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Paper (simulated) keeps the boot/HTTP smoke runner — guards the live-gate
    from over-disabling it."""
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "paper")
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "smoke_paper.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_WS_CLIENT_ENABLED", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET",
        status_code=200,
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
    assert daemon.smoke_runner is not None  # simulated → smoke chain self-test wired
    # Paper/shadow MUST never construct or contend a single-writer advisory lock:
    # build_daemon gates writer_lock on `not spec.is_simulated`, so two shadow/paper
    # instances provably can't contend a lock (and the writer_lock liveness sub-task
    # is skipped in run()). Pin it so a future wiring change can't silently flip it.
    assert daemon.writer_lock is None


@pytest.mark.asyncio
async def test_smoke_runner_gated_off_for_live_executor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Canary/live executor must NOT wire the SmokeRunner: its chain self-test
    submits with placeholder creds, which against the real venue returns
    `10100 apikey: digest invalid`. Smoke is a simulated-only self-test."""
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "smoke_live.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET",
        status_code=200,
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
    assert daemon.smoke_runner is None  # live → smoke self-test disabled (no real-venue probe)


@pytest.mark.asyncio
async def test_canary_build_wires_writer_lock_and_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Live (canary/bitfinex_live) build constructs the WriterLock and appends
    the fail-closed writer_lock guard to the safety chain. On a sqlite
    DATABASE_URL the lock is CONSTRUCTED but never ACQUIRED (acquire is
    Postgres-only) — so this asserts wiring without touching a real lock.
    Paper/shadow leave writer_lock None (covered implicitly by the sibling
    paper test which exercises the simulated path)."""
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "writer_lock_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET",
        status_code=200,
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
    # Constructed in the live block (but NOT acquired, since sqlite).
    assert daemon.writer_lock is not None
    # Fail-closed guard appended whenever live.
    assert any(g.name == "writer_lock" for g in daemon.safety_chain.guards)
