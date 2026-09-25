"""build_daemon wires the trading-status service to the REAL guard chain.

The unit tests above prove each piece behaves; this proves the pieces are
connected. That distinction is the whole point of the endpoint: on 2026-07-27
the components were all individually fine and the failure lived entirely in
what was actually bound at runtime.

Two properties are pinned:
- the service reports the daemon's own safety chain (not a copy), so it can
  never describe guards the money path does not run;
- the effective cap it reports is the one the guards would enforce, including
  which tier bound it.
"""
from __future__ import annotations

import re
from pathlib import Path

from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
from tests.modules.marketfeed.account_test_helpers import (
    configure_account_env,
    seed_exchange_account,
)

_CELLS_YAML = """
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
"""

_SAFETY_YAML = """
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: false}
  heartbeat: {enabled: false, sub_task_stale_threshold_seconds: 300}
  allocation_cap:
    enabled: true
    caps: {fUSD: 1234}
    default_cap: 0
  buying_power:
    enabled: true
    buffers: {fUSD: 3}
    default_buffer: 0
calibrated_guards:
  realized_loss_24h: {enabled: false, threshold_pct: null}
  drawdown_from_peak: {enabled: false, threshold_pct: null}
  divergence_rate: {enabled: false, threshold_pct: null, window_minutes: null}
"""


async def _build(monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock, *, active: bool = True):
    cells = tmp_path / "cells.yaml"
    cells.write_text(_CELLS_YAML)
    safety = tmp_path / "safety.yaml"
    safety.write_text(_SAFETY_YAML)

    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "paper")
    db_path = tmp_path / "daemon_status.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(eng)
    if active:
        # An operator's explicit ACTIVE: without any decision the account is
        # treated as HALTED and nothing trades.
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
        from tests.modules.marketfeed.account_test_helpers import TEST_EXCHANGE_ACCOUNT_ID
        await TradingStateRepository(async_sessionmaker(eng, expire_on_commit=False),
            account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci",
        ).transition("ACTIVE", cause="operator", reason="fixture: trading", actor="test")
    await eng.dispose()

    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety))
    # The legacy scalar every configured symbol overrides — exactly the shape
    # that made it look like a pause while binding nothing.
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "0")
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )
    return await build_daemon(cells_yaml_path=cells, skip_ws=True)


async def test_service_is_wired_and_reads_the_daemons_own_chain(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None
    snap = await daemon.trading_status.snapshot()
    # Same guard objects the reconciler evaluates against.
    assert [g["name"] for g in snap["guards"]] == [
        g.name for g in daemon.safety_chain.guards
    ]
    assert "manual_kill" in [g["name"] for g in snap["guards"]]
    assert snap["trading_readiness"] == {
        "trading_ready": False,
        "reason": "startup_not_ready",
    }


async def test_reported_cap_is_the_one_the_guards_enforce(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None
    snap = await daemon.trading_status.snapshot()
    assert snap["symbols"]["fUSD"]["cap"] == {"value": "1234", "source": "symbol_map"}
    # The env scalar is 0 and binds nothing — the report says so explicitly
    # instead of leaving an operator to infer it from three layers of code.
    assert snap["env_fallback_cap"]["value"] == "0"
    assert snap["env_fallback_cap"]["binding"] is False


async def test_dry_run_passes_when_nothing_blocks(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """The negative control. Without it, a probe that always reported
    would_submit=False would look identical to a working halt — the same
    zero-discriminating-power trap as the alarm that fired 100% of the time."""
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None
    dry = await daemon.trading_status.dry_run()
    assert dry["would_submit_any"] is True
    assert dry["symbols"]["fUSD"]["blocked_by"] is None


async def test_last_submit_attempt_starts_empty_with_a_process_start_time(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None
    snap = await daemon.trading_status.snapshot()
    assert snap["last_submit_attempt"] is None
    assert snap["process_started_at"] is not None


async def test_persisted_halt_blocks_with_the_env_flag_absent(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """The property P2 exists for: this is what a reverted canary.env looks
    like. Before the persisted halt, that revert silently resumed lending."""
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None

    # Baseline: an operator recorded ACTIVE, so trading is live.
    assert (await daemon.trading_status.dry_run())["would_submit_any"] is True

    await daemon.trading_status.halt(reason="candle distortion", actor="test")

    dry = await daemon.trading_status.dry_run()
    assert dry["would_submit_any"] is False
    assert dry["symbols"]["fUSD"]["blocked_by"] == "manual_kill"

    snap = await daemon.trading_status.snapshot()
    assert snap["halt"]["halted"] is True
    assert snap["halt"]["sources"]["persisted"]["reason"] == "candle distortion"


async def test_kill_is_not_lifted_by_the_admin_token(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """/admin/halt is the kill: HALTED, whose exit is an authenticated resume.
    Paper has no venue, so the cancel-all is recorded as skipped, not done."""
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None

    out = await daemon.trading_status.halt(reason="venue incident", actor="test")
    assert (out["state"], out["cause"]) == ("HALTED", "operator")
    assert out["cancel_all_complete"] is False
    assert {(o["phase"], o["detail"]) for o in out["cancel_all"]} == {("skipped", "no_live_venue")}
    assert not hasattr(daemon.trading_status, "resume")  # only TOTP resumes
    assert (await daemon.trading_status.dry_run())["would_submit_any"] is False


async def test_a_halt_leaves_an_audit_trail(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None

    await daemon.trading_status.halt(reason="candle distortion", actor="test")

    assert (await daemon.trading_status.dry_run())["would_submit_any"] is False
    snap = await daemon.trading_status.snapshot()
    # Both transitions retained, newest first.
    assert [(h["halted"], h["reason"]) for h in snap["halt"]["history"]] == [
        (True, "candle distortion"), (False, "fixture: trading"),
    ]


async def test_no_recorded_decision_reads_as_halted(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Fail closed: a scope nobody has decided about does not trade, and the
    admin token cannot lift that any more than it lifts a HALTED."""
    daemon = await _build(monkeypatch, tmp_path, httpx_mock, active=False)
    assert daemon.trading_status is not None
    dry = await daemon.trading_status.dry_run()
    assert dry["would_submit_any"] is False
    assert dry["symbols"]["fUSD"]["blocked_by"] == "manual_kill"
    snap = await daemon.trading_status.snapshot()
    assert snap["halt"]["halted"] is True
    assert snap["halt"]["sources"]["persisted"] is None
    assert not hasattr(daemon.trading_status, "resume")  # only TOTP resumes
