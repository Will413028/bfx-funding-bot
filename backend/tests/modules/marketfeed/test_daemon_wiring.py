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

from tests.modules.marketfeed.account_test_helpers import (
    configure_account_env,
    configure_live_wiring_env,
    seed_exchange_account,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_policy", [False, True])
@pytest.mark.parametrize("schema_current", [False, True])
async def test_normal_live_boot_halted_two_cells(monkeypatch, tmp_path, httpx_mock, with_policy, schema_current):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    from tests.modules.marketfeed.account_test_helpers import TEST_EXCHANGE_ACCOUNT_ID
    configure_account_env(monkeypatch)
    import os
    for name in list(os.environ):
        if name.startswith("BFX_CANARY_") or name in (
            "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
        ):
            monkeypatch.delenv(name)
    values = {"BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "ci", "BFX_EXECUTOR": "bitfinex_live",
        "BFX_WS_CLIENT_ENABLED": "true", "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0",
        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[3] / "configs/safety.canary.yaml"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'normal.db'}"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    engine = make_async_engine_from_url(values["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine, capital_policies=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    halt = TradingStateRepository(factory, account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci")
    await halt.transition("HALTED", cause="operator", reason="retained halt", actor="test")
    if with_policy:
        repo = CapitalRepository(account_id=TEST_EXCHANGE_ACCOUNT_ID, environment="ci", max_snapshot_age_ms=10000)
        async with factory.begin() as session:
            await repo.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                    expected_revision=0, source={"fixture": True})
            await repo.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                    expected_revision=0, source={"fixture": True})
    if not schema_current:
        from sqlalchemy import text
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = 'a7f3c1d9e204'"))
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
                            method="GET", json=[], is_reusable=True, is_optional=True)
    path = _write_cells_yaml(tmp_path)
    import yaml
    doc = yaml.safe_load(path.read_text())
    doc["cells"].append({**doc["cells"][0], "period_agg": "p2"})
    path.write_text(yaml.safe_dump(doc))
    try:
        if not schema_current:
            from bfx_funding_bot.core.schema_head import SchemaHeadMismatch
            before = await halt.current()
            with pytest.raises(SchemaHeadMismatch, match="database=a7f3c1d9e204"):
                await build_daemon(cells_yaml_path=path, skip_ws=True)
            assert (await halt.current()).id == before.id  # a stop stays the stop it was
            assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]
            return
        if not with_policy:
            with pytest.raises(ValueError, match="policy_unavailable"):
                await build_daemon(cells_yaml_path=path, skip_ws=True)
            return
        daemon = await build_daemon(cells_yaml_path=path, skip_ws=True)
        assert len(daemon.config.cells) == 2
        assert (await halt.current()).state == "HALTED"
        status = await daemon.trading_status.snapshot()
        assert status["halt"]["halted"]
        assert "capital_policy" in {g["name"] for g in status["guards"]}
        assert not ({"allocation_cap", "buying_power"} & {g["name"] for g in status["guards"]})
        # The only allowed POST is the public read-only FX calculation, never
        # a financial command. Keep unknown requests fatal in this fixture.
        fx_url = "https://api-pub.bitfinex.com/v2/calc/fx"
        httpx_mock.add_response(url=fx_url, method="POST", json=[0.999865],
                                match_json={"ccy1": "UST", "ccy2": "USD"})
        await daemon.periodic_reconcile._deployment.deploy()
        posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
        assert len(posts) == 1 and str(posts[0].url) == fx_url
        assert not any(name in posts[0].headers for name in (
            "authorization", "bfx-apikey", "bfx-signature", "cookie",
        ))
        assert (await halt.current()).state == "HALTED"

        # A later periodic observation must not supersede the boot snapshot
        # without updating canonical capital. Exercise the assembled recovery
        # chain and real DB; only the venue HTTP boundary is simulated.
        httpx_mock.add_response(
            url=re.compile(r"https://api\.bitfinex\.com/v2/auth/r/funding/(offers|credits|loans).*"),
            method="POST", json=[], is_reusable=True,
        )
        httpx_mock.add_response(
            url="https://api.bitfinex.com/v2/auth/r/wallets",
            method="POST", json=[["funding", "UST", 1000, 0, 1000]], is_reusable=True,
        )
        from time import time_ns

        from sqlalchemy import func, select

        from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

        previous_seq = 0
        for recovery in (daemon.boot_recovery, daemon.periodic_reconcile._recovery,
                         daemon.periodic_reconcile._recovery):
            await recovery.run()
            async with factory.begin() as session:
                capital = await repo.read_capital(
                    session, symbol="fUST", cell_id="fUST_a30", now_ms=time_ns() // 1_000_000,
                )
                latest = await session.scalar(select(func.max(EventLogRow.event_seq)).where(
                    EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED",
                ))
                assert capital.snapshot_seq == latest
                assert capital.snapshot_seq > previous_seq
                assert capital.budget.spendable == Decimal("1000")
                previous_seq = capital.snapshot_seq
            assert (await halt.current()).state == "HALTED"

        # One changed wallet observation must invalidate authority, not reuse
        # the previous successful snapshot or turn the persistent halt off.
        from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError

        httpx_mock.add_response(
            url="https://api.bitfinex.com/v2/auth/r/wallets",
            method="POST", json=[["funding", "UST", 800, 0, 800]],
        )
        httpx_mock.add_response(
            url="https://api.bitfinex.com/v2/auth/r/wallets",
            method="POST", json=[["funding", "UST", 1000, 0, 1000]],
        )
        with pytest.raises(CapitalBlockedError, match="snapshot_unstable"):
            await daemon.periodic_reconcile._recovery.run()
        async with factory.begin() as session:
            with pytest.raises(CapitalBlockedError, match="snapshot_query_pending"):
                await repo.read_capital(
                    session, symbol="fUST", cell_id="fUST_a30", now_ms=time_ns() // 1_000_000,
                )
        assert (await halt.current()).state == "HALTED"
        assert all("/auth/w/" not in str(r.url) for r in httpx_mock.get_requests())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_lock_held", [True, False])
async def test_env_kill_switch_engages_the_kill_at_boot(monkeypatch, tmp_path, httpx_mock, writer_lock_held):
    """BFX_KILL_SWITCH is a real kill at boot: the durable HALTED first, then the
    venue funding cancel-all for each currency -- before any task can trade.
    Without the writer lock the stop is still written and the venue untouched."""
    import json as _json

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    from bfx_funding_bot.core.writer_lock import WriterLock
    from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
    from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    from tests.modules.marketfeed.account_test_helpers import TEST_EXCHANGE_ACCOUNT_ID
    configure_account_env(monkeypatch)
    import os
    for name in list(os.environ):
        if name.startswith("BFX_CANARY_") or name in (
            "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
        ):
            monkeypatch.delenv(name)
    values = {"BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "ci", "BFX_EXECUTOR": "bitfinex_live",
        "BFX_WS_CLIENT_ENABLED": "true", "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0", "BFX_KILL_SWITCH": "true",
        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[3] / "configs/safety.canary.yaml"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'kill.db'}"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    engine = make_async_engine_from_url(values["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine, capital_policies=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trading = TradingStateRepository(factory, account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci")
    await trading.transition("ACTIVE", cause="operator", reason="trading before the kill", actor="test")
    repo = CapitalRepository(account_id=TEST_EXCHANGE_ACCOUNT_ID, environment="ci", max_snapshot_age_ms=10000)
    async with factory.begin() as session:
        await repo.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                expected_revision=0, source={"fixture": True})
        await repo.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                expected_revision=0, source={"fixture": True})

    async def held(self):
        return writer_lock_held
    monkeypatch.setattr(WriterLock, "verify_held", held)
    cancel_all = "https://api.bitfinex.com/v2/auth/w/funding/offer/cancel/all"
    if writer_lock_held:
        httpx_mock.add_response(url=cancel_all, method="POST",
            json=[1, "foc_all-req", None, None, None, None, "SUCCESS", "Cancelled all"])
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
                            method="GET", json=[], is_reusable=True, is_optional=True)
    try:
        daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
        state = await trading.current()
        assert (state.state, state.cause, state.actor) == ("HALTED", "kill_switch", "env:BFX_KILL_SWITCH")
        # Automatic protections are wired where production raises them.
        protection = daemon.protection
        assert protection is not None and daemon.writer_lock_watch is not None
        assert daemon.command_gate is not None and daemon.command_gate.protection is protection
        kill_guard = next(g for g in daemon.safety_chain.guards if g.name == "manual_kill")
        assert kill_guard._pending_stop == protection.pending_reason
        assert protection.pending_reason() is None
        from bfx_funding_bot.modules.execution.events import PositionReconciled
        for at, nav in ((1, "1000"), (2, "900")):  # 10% > the canary's 5% 24h limit
            await daemon.bus.publish(PositionReconciled(
                account_id=str(TEST_EXCHANGE_ACCOUNT_ID), symbol="fUST", reserved=Decimal("0"),
                realized=Decimal("0"), available=Decimal(nav), n_offers=0, n_credits=0,
                occurred_at_ms=at))
        assert (protection.pending_reason() or "").startswith("loss_limiter")
        writes = [r for r in httpx_mock.get_requests() if "/auth/w/" in str(r.url)]
        async with factory() as session:
            phases = [(row.currency, row.phase) for row in (await session.scalars(
                select(FundingCancelAllAuditRow).order_by(FundingCancelAllAuditRow.id))).all()]
        if writer_lock_held:
            assert [(str(r.url), _json.loads(r.content)) for r in writes] == [(cancel_all, {"currency": "UST"})]
            assert phases == [("UST", "requested"), ("UST", "acknowledged")]
        else:
            assert writes == []
            assert phases == [("UST", "skipped")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("deploy_env", "expected"), [
    ({"BFX_IMAGE_DIGEST": "sha256:" + "a" * 64, "BFX_SOURCE_REVISION": "c" * 40,
      "BFX_CHANGE_CLASS": "standard"}, ("ACTIVE", "operator", "kept")),
    ({"BFX_IMAGE_DIGEST": "sha256:" + "a" * 64, "BFX_SOURCE_REVISION": "c" * 40,
      "BFX_CHANGE_CLASS": "material"}, ("REDUCING", "material_deploy", "reducing")),
    ({}, ("REDUCING", "material_deploy", "reducing")),  # no deploy identity: fail closed
])
async def test_live_boot_applies_the_change_class_gate(monkeypatch, tmp_path, httpx_mock, deploy_env, expected):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
    from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    from tests.modules.marketfeed.account_test_helpers import TEST_EXCHANGE_ACCOUNT_ID
    configure_account_env(monkeypatch)
    import os
    for name in list(os.environ):
        if name.startswith("BFX_CANARY_") or name in (
            "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
            "BFX_KILL_SWITCH", "BFX_IMAGE_DIGEST", "BFX_SOURCE_REVISION", "BFX_CHANGE_CLASS",
        ):
            monkeypatch.delenv(name)
    values = {"BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "ci", "BFX_EXECUTOR": "bitfinex_live",
        "BFX_WS_CLIENT_ENABLED": "true", "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0",
        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[3] / "configs/safety.canary.yaml"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}", **deploy_env}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    engine = make_async_engine_from_url(values["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine, capital_policies=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trading = TradingStateRepository(factory, account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci")
    await trading.transition("ACTIVE", cause="operator", reason="trading before the deploy", actor="test")
    repo = CapitalRepository(account_id=TEST_EXCHANGE_ACCOUNT_ID, environment="ci", max_snapshot_age_ms=10000)
    async with factory.begin() as session:
        await repo.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                expected_revision=0, source={"fixture": True})
        await repo.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                expected_revision=0, source={"fixture": True})
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
                            method="GET", json=[], is_reusable=True, is_optional=True)
    try:
        daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
        state = await trading.current()
        assert (state.state, state.cause) == expected[:2]
        status = await daemon.trading_status.snapshot()
        assert status["deployment"]["boot_gate"] == expected[2]
        assert daemon.trading_control is not None
        assert daemon.trading_control.identity.backend_digest == deploy_env.get("BFX_IMAGE_DIGEST")
        # Every decision's audit names the build the deploy tool injected.
        audit = daemon.periodic_reconcile._deployment._audit_context_factory
        assert (audit.service_version, audit.config_hash) == (
            deploy_env.get("BFX_SOURCE_REVISION", "unidentified"),
            deploy_env.get("BFX_IMAGE_DIGEST", "unidentified"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_boot_on_another_schema_stops_trading_and_refuses(monkeypatch, tmp_path, httpx_mock):
    """The wrong build for this database (e.g. a rollback onto a newer schema):
    HALTED/auto before anything can trade, then the boot is refused."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    from bfx_funding_bot.core.schema_head import SCHEMA_HEAD, SchemaHeadMismatch
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    from tests.modules.marketfeed.account_test_helpers import TEST_EXCHANGE_ACCOUNT_ID
    configure_account_env(monkeypatch)
    values = {"BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "ci", "BFX_EXECUTOR": "bitfinex_live",
        "BFX_WS_CLIENT_ENABLED": "true", "BFX_EXECUTION_POLICY": "book_guarded", "BFX_HEALTHZ_PORT": "0",
        "BFX_BOOK_MAX_AGE_SECONDS": "30", "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15",
        "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[3] / "configs/safety.canary.yaml"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    engine = make_async_engine_from_url(values["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    trading = TradingStateRepository(factory, account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci")
    await trading.transition("ACTIVE", cause="operator", reason="trading before the deploy", actor="test")
    try:
        with pytest.raises(SchemaHeadMismatch, match=f"database=ffffffffffff build={SCHEMA_HEAD}"):
            await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
        state = await trading.current()
        assert (state.state, state.cause, state.actor) == ("HALTED", "auto", "boot")
        assert "schema_head_mismatch" in state.reason
        assert not [r for r in httpx_mock.get_requests() if r.method == "POST"]
    finally:
        await engine.dispose()


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
    [("paper", "ci"), ("shadow", "shadow"), ("live", "prod")],
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
    if phase == "live":
        safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
        monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")  # avoid git subprocess
    # Phase 4.4c: file-based sqlite so event-store tables created below are
    # visible to build_daemon's engine (from_snapshot uses them at boot).
    db_path = tmp_path / "daemon_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    if phase == "live":
        configure_live_wiring_env(monkeypatch, tmp_path)
        monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
        monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await seed_exchange_account(_eng)
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
    configure_live_wiring_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
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
    await seed_exchange_account(_eng)
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
    configure_live_wiring_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
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
    await seed_exchange_account(_eng)
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
    configure_live_wiring_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine)
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
    configure_account_env(monkeypatch)
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
    await seed_exchange_account(_eng)
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
    configure_live_wiring_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await seed_exchange_account(_eng)
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
    configure_live_wiring_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await seed_exchange_account(_eng)
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
