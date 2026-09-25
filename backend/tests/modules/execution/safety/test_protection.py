"""Automatic protections (ladder level 3): tripping, the managed kill, what never trips.

The replay section feeds the five production divergences of 2026-08-27 ..
2026-09-24 (all ``released=0 claimed=0 failed=0 reserved_drift=0``) through the
same classifier the recovery uses; none may halt.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.event_store.store import SymbolLedgerDelta
from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.safety.kill_switch import CancelAllOutcome, KillResult
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import ReconcileNavTracker
from bfx_funding_bot.modules.execution.safety.protection import (
    COMMAND_RATE_EXCEEDED,
    IDENTITY_CONFLICT,
    NAV_DROP,
    OFFER_AMOUNT_MISMATCH,
    TRIGGERS,
    AutomaticProtection,
    LedgerConservation,
    NavDropMonitor,
    WriterLockLostError,
    WriterLockWatch,
)
from bfx_funding_bot.modules.execution.safety.trading_state import TradingState

D = Decimal


class FakeKill:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.failures = failures

    async def engage(self, *, cause: str, actor: str, reason: str,
                     when_already_halted: str = "retry", scope: str = "all") -> KillResult:
        assert when_already_halted == "skip"  # automatic kills never re-run a kill in force
        assert scope == "managed"  # and never touch offers placed by hand
        self.calls.append((cause, actor, reason))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("database unavailable")
        state = TradingState(id=9, state="HALTED", cause=cause, actor=actor, reason=reason,
                             created_at_ms=1)
        return KillResult(state=state, state_changed=True,
                          cancel_all=(CancelAllOutcome("UST", "acknowledged"),))


class Recorder:
    def __init__(self) -> None:
        self.trips: list[tuple[str, str]] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.trips.append((trigger, detail))


# ------------------------------------------------------------------ tripping


async def test_trip_stops_at_once_and_the_supervised_task_kills_with_cause_auto() -> None:
    protection = AutomaticProtection(clock=lambda: 7)
    kill = FakeKill()
    protection.bind(kill)
    assert protection.pending_reason() is None
    protection.trip(OFFER_AMOUNT_MISMATCH, "venue_offer_id=1 symbol=fUST")
    assert protection.pending_reason() == "offer_amount_mismatch: venue_offer_id=1 symbol=fUST"
    stop = asyncio.Event()
    task = asyncio.create_task(protection.run(stop))
    for _ in range(100):
        if kill.calls:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert kill.calls == [("auto", "auto:offer_amount_mismatch",
                           "offer_amount_mismatch: venue_offer_id=1 symbol=fUST")]
    assert protection.pending_reason() is None


async def test_trips_queued_before_the_task_runs_become_one_kill() -> None:
    protection = AutomaticProtection()
    kill = FakeKill()
    protection.bind(kill)
    protection.trip(IDENTITY_CONFLICT, "a")
    protection.trip(COMMAND_RATE_EXCEEDED, "b")
    await protection.run_pending()
    assert len(kill.calls) == 1
    cause, actor, reason = kill.calls[0]
    assert (cause, actor) == ("auto", "auto:identity_conflict")
    assert "command_rate_exceeded: b" in reason


async def test_a_failed_kill_keeps_new_offers_stopped_and_is_retried() -> None:
    protection = AutomaticProtection(retry_s=0.01)
    kill = FakeKill(failures=2)
    protection.bind(kill)
    protection.trip(IDENTITY_CONFLICT, "conflict")
    stop = asyncio.Event()
    task = asyncio.create_task(protection.run(stop))
    for _ in range(300):
        if len(kill.calls) == 3:
            break
        assert protection.pending_reason() is not None
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(kill.calls) == 3
    assert protection.pending_reason() is None


def test_only_bot_integrity_conditions_stop_trading() -> None:
    """Lending envelope D3: UNKNOWN quarantines a symbol, foreign offers and NAV
    drops alert, a lost writer lock exits -- none of them is a stop."""
    from bfx_funding_bot.modules.execution.safety.protection import CAPITAL_BLOCK_TRIGGERS
    assert {"unclassifiable_commitment", "offer_amount_mismatch", "identity_conflict",
                        "venue_lent_above_ledger", "command_rate_exceeded"} == TRIGGERS
    assert "unattributed_offer" not in CAPITAL_BLOCK_TRIGGERS


async def test_an_unbound_protection_still_stops_new_offers() -> None:
    protection = AutomaticProtection()
    protection.trip("not_a_known_trigger", "programming error")
    await protection.run_pending()
    assert protection.pending_reason() is not None


# ------------------------------------------------------ ledger conservation


def _delta(symbol: str, prior_offered: str, prior_lent: str, observed_offered: str,
           observed_lent: str, baseline: bool = True) -> SymbolLedgerDelta:
    return SymbolLedgerDelta(symbol, D(prior_offered), D(prior_lent), D(observed_offered),
                             D(observed_lent), baseline)


def test_replay_2026_09_24_canary_loan_end_does_not_halt() -> None:
    """realized_drift=150.77638588 reserved_drift=0: the canary's 150.78 loan ended."""
    conservation = LedgerConservation()
    assert conservation.observe([_delta("fUST", "0", "150.77638588", "0", "0")],
                                confirmed=True).anomalies == ()


@pytest.mark.parametrize("when", ["2026-08-27 12:14", "2026-08-27 14:51",
                                  "2026-08-28 14:32", "2026-08-30 16:14"])
@pytest.mark.parametrize("reading", ["returned", "first_observation"])
def test_replay_2026_08_migration_catch_ups_do_not_halt(when: str, reading: str) -> None:
    """realized_drift=392.x reserved_drift=0 during the account migration.

    The logged drift is an absolute value, so both readings of "the whole
    balance caught up" are replayed: lent returning to the wallet, and the
    first venue observation of a ledger the migration re-keyed (no baseline).
    A third reading -- lent rising against an established ledger -- WOULD
    halt, deliberately: lending the bot did not do is unexplained whatever
    caused it, because the account is bot-only and auto-renew is off.
    """
    conservation = LedgerConservation()
    delta = (_delta("fUST", "0", "392.4", "0", "0") if reading == "returned"
             else _delta("fUST", "0", "0", "0", "392.4", baseline=False))
    assert conservation.observe([delta], confirmed=True).anomalies == ()


@pytest.mark.parametrize(("delta", "why"), [
    (_delta("fUST", "150", "0", "0", "150"), "fill caught by reconcile instead of WS"),
    (_delta("fUST", "150", "0", "50", "100"), "partial fill caught by reconcile"),
    (_delta("fUST", "150", "0", "0", "0"), "cancel or expiry caught by reconcile"),
    (_delta("fUST", "150", "300", "0", "250"), "fill and a loan end in one interval"),
    (_delta("fUST", "0", "100", "0", "100.005"), "within epsilon"),
])
def test_expected_catch_ups_never_trip(delta: SymbolLedgerDelta, why: str) -> None:
    assert LedgerConservation().observe([delta], confirmed=True).anomalies == (), why


def test_lent_growth_our_offers_cannot_explain_trips() -> None:
    anomalies = LedgerConservation().observe(
        [_delta("fUST", "100", "0", "0", "492.4"), _delta("fUSD", "0", "0", "0", "0")],
        confirmed=True).anomalies
    assert len(anomalies) == 1
    assert "symbol=fUST" in anomalies[0] and "unexplained=392.4" in anomalies[0]


def test_a_double_counted_fill_in_an_unconfirmed_snapshot_cancels_out() -> None:
    """Offers fetched before a fill and credits after it count the money twice;
    such a snapshot is never confirmed, and carrying its delta to the next
    confirmed one nets the artefact out."""
    conservation = LedgerConservation()
    assert conservation.observe([_delta("fUST", "150", "0", "150", "150")], confirmed=False).anomalies == ()
    assert conservation.observe([_delta("fUST", "150", "150", "0", "150")], confirmed=True).anomalies == ()


def test_an_anomaly_absorbed_by_an_unconfirmed_snapshot_is_still_caught() -> None:
    conservation = LedgerConservation()
    assert conservation.observe([_delta("fUST", "0", "0", "0", "392.4")], confirmed=False).anomalies == ()
    # The unconfirmed snapshot already reset the ledger to 392.4.
    anomalies = conservation.observe([_delta("fUST", "0", "392.4", "0", "392.4")],
                                     confirmed=True).anomalies
    assert anomalies and "unexplained=392.4" in anomalies[0]


def test_lending_a_foreign_offer_explains_is_an_alert_not_a_stop() -> None:
    """D2: a manual offer placed and filled between two snapshots was never seen
    as an offer; its executed history explains the new lending."""
    verdict = LedgerConservation().observe([_delta("fUST", "0", "0", "0", "200")], confirmed=True,
                                           foreign_executed={"fUST": D("200")})
    assert verdict.anomalies == () and len(verdict.foreign) == 1
    assert "foreign_executed=200" in verdict.foreign[0]


def test_lending_beyond_what_foreign_fills_explain_still_trips() -> None:
    verdict = LedgerConservation().observe([_delta("fUST", "0", "0", "0", "350")], confirmed=True,
                                           foreign_executed={"fUST": D("200"), "fUSD": D("500")})
    assert verdict.foreign == () and len(verdict.anomalies) == 1
    assert "unexplained=350" in verdict.anomalies[0]


def test_an_unconfirmed_snapshot_alone_never_trips() -> None:
    assert LedgerConservation().observe([_delta("fUST", "0", "0", "0", "392.4")],
                                        confirmed=False).anomalies == ()


# ------------------------------------------------------------ NAV drop (alert)


def _reconciled(symbol: str, nav: str, at: int) -> PositionReconciled:
    return PositionReconciled(account_id="acct", symbol=symbol, reserved=D("0"), realized=D("0"),
                              available=D(nav), n_offers=0, n_credits=0, occurred_at_ms=at)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    from bfx_funding_bot.modules.execution.safety import protection as module
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(module.alerts, "emit", lambda event, **fields: captured.append((event, fields)))
    return captured


async def test_a_nav_drop_alerts_once_per_breach_and_isolates_currencies(sent) -> None:
    monitor = NavDropMonitor(source=ReconcileNavTracker("acct"),
                             realized_loss_threshold_pct=5.0, drawdown_threshold_pct=10.0)
    await monitor.on_position_reconciled(_reconciled("fUST", "1000", 1))
    await monitor.on_position_reconciled(_reconciled("fUSD", "1000", 1))
    await monitor.on_position_reconciled(_reconciled("fUST", "960", 2))  # 4% < 5%
    assert sent == []
    await monitor.on_position_reconciled(_reconciled("fUST", "940", 3))  # 6% > 5%
    # Alerted on the very event that crossed: the tracker updated first.
    assert [(event, fields["symbol"], fields["metric"]) for event, fields in sent] == [
        (NAV_DROP, "fUST", "realized_loss_pct_24h")]
    await monitor.on_position_reconciled(_reconciled("fUST", "939", 4))  # same breach
    await monitor.on_position_reconciled(_reconciled("fUSD", "999", 5))  # unrelated currency
    assert len(sent) == 1


async def test_disabled_nav_thresholds_never_alert(sent) -> None:
    monitor = NavDropMonitor(source=ReconcileNavTracker("acct"),
                             realized_loss_threshold_pct=None, drawdown_threshold_pct=None)
    await monitor.on_position_reconciled(_reconciled("fUST", "1000", 1))
    await monitor.on_position_reconciled(_reconciled("fUST", "1", 2))
    assert sent == []


# -------------------------------------------------------------- writer lock


class FakeLock:
    def __init__(self, results: list[bool]) -> None:
        self.results = results

    async def refresh(self) -> bool:
        return self.results.pop(0)


async def test_a_lost_writer_lock_ends_the_process_instead_of_halting(sent) -> None:
    watch = WriterLockWatch(lock=FakeLock([True, False]))
    assert await watch.check() is True
    with pytest.raises(WriterLockLostError):
        await watch.check()
    assert [event for event, _ in sent] == ["daemon_fatal"]


@pytest.mark.parametrize("reason", [
    "offer_provenance_conflict", "offer_attempt_conflict", "attempt_decision_conflict",
    "attempt_amount_conflict", "attempt_projection_conflict", "attempt_intent_conflict",
    "attempt_intent_scope_conflict", "attempt_outcome_evidence_conflict",
    "attempt_outcome_evidence_identity", "attempt_outcome_evidence_scope",
    "duplicate_attempt_intent", "execution_unknown_resolution_conflict",
    "snapshot_conflicting_identity",
])
def test_identity_conflicts_are_protections(reason: str) -> None:
    from bfx_funding_bot.modules.execution.safety.protection import CAPITAL_BLOCK_TRIGGERS
    assert CAPITAL_BLOCK_TRIGGERS[reason] == "identity_conflict"


@pytest.mark.parametrize("reason", [
    "snapshot_query_pending", "snapshot_unstable", "snapshot_stale",
    "snapshot_command_fence_changed", "execution_unknown", "policy_unavailable",
])
def test_transient_observation_states_are_not_protections(reason: str) -> None:
    from bfx_funding_bot.modules.execution.safety.protection import CAPITAL_BLOCK_TRIGGERS
    assert reason not in CAPITAL_BLOCK_TRIGGERS
