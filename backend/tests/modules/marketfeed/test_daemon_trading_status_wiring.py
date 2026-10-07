"""build_daemon wires the trading-status service to the REAL guard chain.

The unit tests above prove each piece behaves; this proves the pieces are
connected. That distinction is the whole point of the endpoint: on 2026-07-27
the components were all individually fine and the failure lived entirely in
what was actually bound at runtime.

Two properties are pinned:
- the service reports the daemon's own safety chain (not a copy), so it can
  never describe guards the money path does not run;
- the capital it reports is the applied authority's, never a legacy env scalar
  or map.
"""
from __future__ import annotations

from pathlib import Path

from pytest_httpx import HTTPXMock
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.writer_lock import WriterLock
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    boot_live_construction,
)


async def _build(monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock, *, active: bool = True):
    async def operator_active(engine) -> None:
        # An operator's explicit ACTIVE: without any decision the account is
        # treated as HALTED and nothing trades.
        await TradingStateRepository(async_sessionmaker(engine, expire_on_commit=False),
            account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci",
        ).transition("ACTIVE", cause="operator", reason="fixture: trading", actor="test")

    daemon, engine = await boot_live_construction(
        monkeypatch, tmp_path, httpx_mock, name="daemon_status",
        before_boot=operator_active if active else None,
    )
    await engine.dispose()
    return daemon


def _guard(snapshot_or_dry: dict, symbol: str, name: str) -> dict:
    return next(g for g in snapshot_or_dry["symbols"][symbol]["guards"] if g["name"] == name)


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
    # Before its first answer the database is a never-seen dependency (fail-closed).
    assert snap["trading_readiness"] == {
        "trading_ready": False,
        "reason": "dependency_stale",
    }


async def test_reported_capital_is_the_applied_authoritys_not_a_legacy_tier(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None
    snap = await daemon.trading_status.snapshot()
    # No venue snapshot yet: the authority answers "no capital", with its reason.
    fust = snap["symbols"]["fUST"]
    assert fust["capital_available"] is False and fust["reason"]
    assert "cap" not in fust and "buffer" not in fust
    assert "env_fallback_cap" not in snap and "env_fallback_buffer" not in snap
    assert snap["deployment_environment"] == "ci"
    assert snap["account_id"] == str(TEST_EXCHANGE_ACCOUNT_ID)
    assert snap["deployment"] is not None


async def test_dry_run_reaches_the_capital_guard_when_nothing_halts(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """The negative control. Without it, a probe that always reported
    would_submit=False would look identical to a working halt — the same
    zero-discriminating-power trap as the alarm that fired 100% of the time.
    With an operator ACTIVE the halt guard allows, so any block is the capital guard's."""
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None
    dry = await daemon.trading_status.dry_run()
    assert dry["symbols"]["fUST"]["blocked_by"] != "manual_kill"
    assert _guard(dry, "fUST", "manual_kill")["allowed"] is True


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

    # Baseline: an operator recorded ACTIVE, so the halt guard allows.
    baseline = await daemon.trading_status.dry_run()
    assert _guard(baseline, "fUST", "manual_kill")["allowed"] is True

    async def venue_unreachable(self) -> bool:
        return False

    monkeypatch.setattr(WriterLock, "verify_held", venue_unreachable)  # no venue cancel-all here
    await daemon.trading_status.halt(reason="candle distortion", actor="test")

    dry = await daemon.trading_status.dry_run()
    assert dry["would_submit_any"] is False
    assert dry["symbols"]["fUST"]["blocked_by"] == "manual_kill"

    snap = await daemon.trading_status.snapshot()
    assert snap["halt"]["halted"] is True
    assert snap["halt"]["sources"]["persisted"]["reason"] == "candle distortion"


async def test_kill_is_not_lifted_by_the_admin_token(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """/admin/halt is the kill: HALTED, whose exit is an authenticated resume.
    Without the writer lock the stop is still written and the venue untouched."""
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None

    async def not_held(self) -> bool:
        return False

    monkeypatch.setattr(WriterLock, "verify_held", not_held)
    out = await daemon.trading_status.halt(reason="venue incident", actor="test")
    assert (out["state"], out["cause"]) == ("HALTED", "operator")
    assert out["cancel_all_complete"] is False
    assert {o["phase"] for o in out["cancel_all"]} == {"skipped"}
    assert not hasattr(daemon.trading_status, "resume")  # only TOTP resumes
    assert (await daemon.trading_status.dry_run())["would_submit_any"] is False


async def test_a_halt_leaves_an_audit_trail(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    assert daemon.trading_status is not None

    async def not_held(self) -> bool:
        return False

    monkeypatch.setattr(WriterLock, "verify_held", not_held)
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
    assert dry["symbols"]["fUST"]["blocked_by"] == "manual_kill"
    snap = await daemon.trading_status.snapshot()
    assert snap["halt"]["halted"] is True
    assert snap["halt"]["sources"]["persisted"] is None
    assert not hasattr(daemon.trading_status, "resume")  # only TOTP resumes
