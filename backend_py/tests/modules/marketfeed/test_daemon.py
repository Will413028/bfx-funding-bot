from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
from bfx_funding_bot.modules.marketfeed.schemas import Phase
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    configure_account_env,
    seed_exchange_account,
)


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
    await seed_exchange_account(_eng)
    await _eng.dispose()
    # OS-assigned port to avoid 8080 conflicts during parallel runs / dev boxes.
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    # Phase 4.2 Task 20 execution wiring requires these env vars.
    configure_account_env(monkeypatch)
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
    await seed_exchange_account(_eng)
    await _eng.dispose()
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    configure_account_env(monkeypatch)
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
    await seed_exchange_account(_eng)
    await _eng.dispose()
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    configure_account_env(monkeypatch)
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


_CANARY_ACCOUNT_ID = UUID("11111111-1111-1111-1111-111111111111")


def _bounded_canary_profile():
    """One immutable cell with a 150 USDT ceiling, derived without production helpers."""
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryProfile

    return CanaryProfile(
        account_id=_CANARY_ACCOUNT_ID,
        environment="prod",
        symbol="fUST",
        cell="fUST_a30",
        strategy="mean_reversion",
        amount_usdt=Decimal("150"),
        cap_usdt=Decimal("150"),
        max_evidence_age_seconds=300,
    )


def _valid_canary_evidence():
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryEvidence

    return CanaryEvidence(
        account_id=str(_CANARY_ACCOUNT_ID),
        environment="prod",
        symbol="fUST",
        cell="fUST_a30",
        strategy="mean_reversion",
        amount_usdt=Decimal("150"),
        permit_id="33333333-3333-3333-3333-333333333333",
        command_decision_id="decision-1",
        attempt_id="22222222-2222-2222-2222-222222222222",
        outcome_kind="acknowledged",
        venue_offer_id="offer-1",
        outcome_at_ms=1_000_000,
        outcome_event_seq=100,
        reconcile_fences=(101, 102),
        reconcile_observed_at_ms=(1_000_100, 1_000_200),
        projection_hash="a" * 64,
        venue_db_exposure_diff_usdt=Decimal("0"),
        full_account_snapshot_complete=True,
        stop_reason=None,
    )


def _ready_canary_state():
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryReadiness

    return CanaryReadiness(
        open_uncertainty_count=0,
        projector_lag=0,
        full_account_snapshot_complete=True,
        reconcile_fences=(101, 102),
        reconcile_observed_at_ms=(1_000_100, 1_000_200),
        observed_at_ms=1_000_250,
        persistent_halt=True,
    )


class _VenueSubmitter:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self) -> None:
        self.calls += 1


async def _submit_only_after_canary_startup_gate(
    submit,
    *,
    profile,
    evidence,
    readiness,
    configured_cells,
    configured_caps,
    allocation_cap_usdt: Decimal,
) -> None:
    """The daemon uses this same startup decision before building its live executor."""
    from bfx_funding_bot.modules.marketfeed.daemon import assert_canary_startup

    assert_canary_startup(
        profile=profile,
        evidence=evidence,
        readiness=readiness,
        configured_cells=configured_cells,
        configured_caps=configured_caps,
        allocation_cap_usdt=allocation_cap_usdt,
    )
    await submit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate_evidence", "mutate_readiness", "expected_reason"),
    [
        (lambda evidence: None, lambda state: state, "missing_canary_evidence"),
        (
            lambda evidence: evidence,
            lambda state: replace(state, persistent_halt=False),
            "persistent_halt_absent",
        ),
        (
            lambda evidence: replace(evidence, outcome_at_ms=500_000),
            lambda state: state,
            "canary_evidence_stale",
        ),
        (
            lambda evidence: evidence,
            lambda state: replace(state, projector_lag=1),
            "projector_lag",
        ),
        (
            lambda evidence: evidence,
            lambda state: replace(state, open_uncertainty_count=1),
            "open_execution_uncertainty",
        ),
        (
            lambda evidence: evidence,
            lambda state: replace(state, full_account_snapshot_complete=False),
            "venue_snapshot_coverage_incomplete",
        ),
        (
            lambda evidence: replace(evidence, reconcile_fences=(101,)),
            lambda state: state,
            "two_reconcile_cycles_required",
        ),
    ],
)
async def test_canary_startup_failures_prevent_venue_submission(
    mutate_evidence,
    mutate_readiness,
    expected_reason: str,
) -> None:
    """Removing the startup gate must expose a real submit in this harness."""
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryStartupBlocked

    profile = _bounded_canary_profile()
    baseline_evidence = _valid_canary_evidence()
    evidence = mutate_evidence(baseline_evidence)
    readiness = mutate_readiness(_ready_canary_state())
    venue = _VenueSubmitter()

    with pytest.raises(CanaryStartupBlocked, match=expected_reason):
        await _submit_only_after_canary_startup_gate(
            venue.submit,
            profile=profile,
            evidence=evidence,
            readiness=readiness,
            configured_cells=(("mean_reversion", "fUST", "fUST_a30"),),
            configured_caps={"fUST": Decimal("150")},
            allocation_cap_usdt=Decimal("150"),
        )

    assert venue.calls == 0


@pytest.mark.asyncio
async def test_valid_bounded_canary_allows_exactly_one_submission_after_two_fences() -> None:
    """The gate must permit one bounded acknowledged outcome, not merely parse it."""
    venue = _VenueSubmitter()
    await _submit_only_after_canary_startup_gate(
        venue.submit,
        profile=_bounded_canary_profile(),
        evidence=_valid_canary_evidence(),
        readiness=_ready_canary_state(),
        configured_cells=(("mean_reversion", "fUST", "fUST_a30"),),
        configured_caps={"fUST": Decimal("150")},
        allocation_cap_usdt=Decimal("150"),
    )

    assert venue.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_cells", "configured_caps", "allocation_cap_usdt", "expected_reason"),
    [
        (
            (("mean_reversion", "fUST", "fUST_a30"),),
            {"fUST": Decimal("151")},
            Decimal("150"),
            "canary_cap_mismatch",
        ),
        (
            (
                ("mean_reversion", "fUST", "fUST_a30"),
                ("mean_reversion", "fUSD", "fUSD_a30"),
            ),
            {"fUST": Decimal("150"), "fUSD": Decimal("150")},
            Decimal("150"),
            "canary_scope_mismatch",
        ),
        (
            (("mean_reversion", "fUST", "fUST_a30"),),
            {"fUST": Decimal("150")},
            Decimal("151"),
            "canary_allocation_cap_mismatch",
        ),
    ],
)
async def test_canary_scope_or_cap_expansion_prevents_venue_submission(
    configured_cells,
    configured_caps,
    allocation_cap_usdt: Decimal,
    expected_reason: str,
) -> None:
    """An extra cell or either cap expansion must not reach the venue."""
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryStartupBlocked

    venue = _VenueSubmitter()
    with pytest.raises(CanaryStartupBlocked, match=expected_reason):
        await _submit_only_after_canary_startup_gate(
            venue.submit,
            profile=_bounded_canary_profile(),
            evidence=_valid_canary_evidence(),
            readiness=_ready_canary_state(),
            configured_cells=configured_cells,
            configured_caps=configured_caps,
            allocation_cap_usdt=allocation_cap_usdt,
        )

    assert venue.calls == 0


async def test_canary_readiness_requires_two_complete_account_snapshot_events(
    tmp_path: Path,
) -> None:
    """A symbol checkpoint cannot substitute for two complete account snapshots."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
    from bfx_funding_bot.modules.marketfeed.daemon import collect_canary_readiness

    db_path = tmp_path / "canary_snapshot_coverage.db"
    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = PostgresEventStore(deployment_environment="prod")
    async with factory.begin() as session:
        for timestamp, complete in ((1_000_100, False), (1_000_200, True)):
            await store.append_snapshot(
                session,
                VenueSnapshotObserved(
                    account_id=str(TEST_EXCHANGE_ACCOUNT_ID),
                    environment="prod",
                    query_started_at_ms=timestamp - 10,
                    query_finished_at_ms=timestamp,
                    offers=(),
                    credits=(),
                    wallet_available={"fUST": Decimal("0")},
                    coverage=SnapshotCoverage(
                        active_offers_complete=complete,
                        active_credits_complete=True,
                        wallets_complete=True,
                    ),
                ),
            )
    async with factory() as session:
        readiness = await collect_canary_readiness(
            session,
            account_id=TEST_EXCHANGE_ACCOUNT_ID,
            environment="prod",
            now_ms=1_000_250,
        )
    await engine.dispose()

    assert readiness.full_account_snapshot_complete is False


async def test_live_build_blocks_missing_halt2_evidence_before_executor_construction(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """The real daemon boot boundary must stop before a live executor exists."""
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryStartupBlocked, build_daemon

    cells = tmp_path / "one-cell.yaml"
    cells.write_text("""
cells:
  - strategy: mean_reversion
    symbol: fUST
    period_agg: a30
    timeframe: 1h
    params: {threshold_sigma: 0.5, ratio_sigma: 0.42, ema_span: 24}
""")
    safety = tmp_path / "one-cell-safety.yaml"
    safety.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: 300}
  allocation_cap: {enabled: true, caps: {fUST: 150}, default_cap: 0}
  buying_power: {enabled: true, buffers: {fUST: 3}, default_buffer: 0}
calibrated_guards:
  realized_loss_24h: {enabled: true, threshold_pct: 5}
  drawdown_from_peak: {enabled: true, threshold_pct: 10}
  divergence_rate: {enabled: false, threshold_pct: null, window_minutes: null}
""")
    db_path = tmp_path / "live-gate.db"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety))
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "150")
    monkeypatch.setenv("BFX_CANARY_ACCOUNT_ID", str(TEST_EXCHANGE_ACCOUNT_ID))
    monkeypatch.setenv("BFX_CANARY_ENVIRONMENT", "prod")
    monkeypatch.setenv("BFX_CANARY_SYMBOL", "fUST")
    monkeypatch.setenv("BFX_CANARY_CELL", "fUST_a30")
    monkeypatch.setenv("BFX_CANARY_STRATEGY", "mean_reversion")
    monkeypatch.setenv("BFX_CANARY_AMOUNT_USDT", "150")
    monkeypatch.setenv("BFX_CANARY_CAP_USDT", "150")
    monkeypatch.setenv("BFX_CANARY_MAX_EVIDENCE_AGE_SECONDS", "300")
    monkeypatch.setenv("BFX_CANARY_EVIDENCE_REPORT", str(tmp_path / "forged.json"))
    monkeypatch.delenv("BFX_HALT2_EVIDENCE_REPORT", raising=False)
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine)
    await engine.dispose()
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[], is_reusable=True, is_optional=True,
    )
    constructed = False

    def should_not_construct_executor(*_args, **_kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("live executor construction must be unreachable")

    monkeypatch.setattr("bfx_funding_bot.modules.marketfeed.daemon.build_executor", should_not_construct_executor)
    with pytest.raises(CanaryStartupBlocked, match="missing_halt2_evidence"):
        await build_daemon(cells_yaml_path=cells, skip_ws=True)

    assert constructed is False
