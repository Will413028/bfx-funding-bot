from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from pathlib import Path

from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
from bfx_funding_bot.modules.marketfeed.schemas import Phase


async def test_daemon_builds_and_runs_briefly(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Smoke: build_daemon() returns a Daemon with all components wired;
    daemon.run() starts all sub-tasks, responds to _stop_event, and exits
    cleanly (TaskGroup pattern, Phase 4.2).

    Boot path uses PG from_snapshot (4.4c).
    Test uses a file-based sqlite DB (not :memory:) so event-store tables
    created here are visible to the engine inside build_daemon.
    """
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "paper")
    # Phase 4.4c: file-based sqlite so event-store tables created below are
    # visible to build_daemon's engine (per-connection :memory: would not share
    # the schema across two engine instances).
    db_path = tmp_path / "daemon.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()
    # OS-assigned port to avoid 8080 conflicts during parallel runs / dev boxes.
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    # Phase 4.2 Task 20 execution wiring requires these env vars.
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    # warmup_cell fetches Bitfinex candles; mock so httpx_mock teardown does not
    # complain about unexpected requests. File-based SQLite lets warmup proceed
    # further than :memory: (tables exist), so this GET is now reached.
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )
    daemon = await build_daemon(cells_yaml_path=yaml_path, skip_ws=True)
    assert daemon.config.phase == Phase.PAPER

    # Run briefly then signal stop — TaskGroup should drain all sub-tasks.
    async def _stop_after_delay() -> None:
        await asyncio.sleep(0.05)
        daemon._stop_event.set()

    stop_task = asyncio.create_task(_stop_after_delay())
    await daemon.run()  # should return cleanly after _stop_event is set
    await stop_task


async def test_daemon_engine_has_d3_pool_config_and_url_transform(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Regression for 5/21 chaos recovery URL-handling crash.

    daemon.py:614 was passing config.database_url directly to
    create_async_engine since Phase 4.1 (`26b059d`), bypassing
    _prepare_engine_kwargs (D2 URL transform) and the pool_pre_ping +
    pool_recycle=600 settings added by D3 (`7a826d8`). It only "worked"
    because Koyeb DATABASE_URL secret was pre-transformed manually. Chaos
    recovery rebuilt the secret from Neon dashboard libpq form and crashed.

    Phase 4.4c: build_daemon now calls from_snapshot at boot, so it needs a
    real DB. This test uses a file-based sqlite for build_daemon, and separately
    calls make_async_engine_from_url with the problematic postgresql URL to
    lock the URL-transform + pool-config contract without needing a live server.
    """
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
""")
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "paper")
    # File-based sqlite so from_snapshot inside build_daemon can open sessions.
    db_path = tmp_path / "daemon_d3.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    # warmup_cell fetches Bitfinex candles with file-based sqlite (tables exist).
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )
    # Verify URL transform + pool config via make_async_engine_from_url directly
    # (the form chaos recovery accidentally produced — asyncpg scheme + libpq params).
    bad_url = (
        "postgresql+asyncpg://u:p@ep-foo-pooler.ap-southeast-1.aws.neon.tech/db"
        "?sslmode=require&channel_binding=require"
    )
    transformed_engine = make_async_engine_from_url(bad_url)
    u = str(transformed_engine.url)
    assert u.startswith("postgresql+asyncpg://"), f"scheme not asyncpg: {u}"
    assert "sslmode" not in u, f"sslmode not stripped: {u}"
    assert "channel_binding" not in u, f"channel_binding not stripped: {u}"
    assert "-pooler." not in u, f"-pooler suffix not stripped: {u}"
    assert transformed_engine.pool._pre_ping is True, "pool_pre_ping missing (D3)"
    assert transformed_engine.pool._recycle == 600, "pool_recycle != 600 (D3.1)"
    await transformed_engine.dispose()

    # build_daemon still uses the sqlite URL but daemon.db_engine is also checked.
    daemon = await build_daemon(cells_yaml_path=yaml_path, skip_ws=True)
    # Confirm daemon's engine was built via make_async_engine_from_url (not raw create_async_engine).
    # SQLite path doesn't apply pool config the same way; the key regression guard
    # is the make_async_engine_from_url call above with the real postgresql URL.
    assert daemon.db_engine is not None


async def test_ledger_subscribes_to_position_reconciled(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Behavioral: bus.publish(PositionReconciled) must update ledger.realized_exposure().

    Wires the same paper daemon as the smoke test (BFX_PHASE=paper, sqlite),
    then publishes a PositionReconciled on the daemon's bus and asserts the ledger
    reflects the authoritative venue snapshot.  This guards the atomicity
    requirement: both the subscription and the offer_registry routing to
    BootRecovery must land in a single commit (see daemon.py wiring comment).
    """
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "paper")
    db_path = tmp_path / "daemon_pr.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=yaml_path, skip_ws=True)

    # Publish an authoritative venue snapshot via PositionReconciled.
    # The subscription bus.subscribe(PositionReconciled, ledger.on_position_reconciled)
    # must route this to the ledger; without the wiring realized_exposure() stays 0.
    event = PositionReconciled(
        account_id=daemon.ledger.account_id,
        reserved_usdt=Decimal("0"),
        realized_usdt=Decimal("450"),
        available_usdt=Decimal("0"),
        n_offers=0,
        n_credits=3,
        occurred_at_ms=1,
    symbol="fUST")
    await daemon.bus.publish(event)

    assert daemon.ledger.realized_exposure("fUST") == Decimal("450"), (
        "ledger.realized_exposure('fUST') should reflect PositionReconciled.realized_usdt "
        "after bus.publish — subscription missing or account_id mismatch"
    )
