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
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import ReconcileNavTracker
from bfx_funding_bot.modules.execution.safety.protection import (
    COMMAND_RATE_EXCEEDED,
    IDENTITY_CONFLICT,
    NAV_DROP,
    OFFER_AMOUNT_MISMATCH,
    TRIGGERS,
    VENUE_LENT_ABOVE_LEDGER,
    AutomaticProtection,
    LedgerConservation,
    NavDropMonitor,
    WriterLockLostError,
    WriterLockWatch,
)
from bfx_funding_bot.modules.execution.safety.trading_state import TradingState, TransitionResult

D = Decimal


class FakeKill:
    """The trading state an automatic protection writes HALTED/auto to."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.failures = failures

    async def transition(self, state: str, *, cause: str, actor: str, reason: str,
                         now_ms: int | None = None) -> TransitionResult:
        assert state == "HALTED"  # a protection only ever stops; the planner then cancels
        self.calls.append((cause, actor, reason))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("database unavailable")
        halted = TradingState(id=9, state="HALTED", cause=cause, actor=actor, reason=reason,
                              created_at_ms=1)
        return TransitionResult(state=halted, changed=True)


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


def test_capital_block_trigger_keeps_every_legacy_reason_and_maps_the_ledger_ones() -> None:
    """G11: one pure reason -> trigger mapping for both authorities. Legacy codes
    keep their triggers exactly; the ledger's integrity codes halt as their
    legacy analogues do; its unbounded tail is transient (retry, never halt)."""
    from bfx_funding_bot.modules.execution.safety.protection import (
        CAPITAL_BLOCK_TRANSIENT,
        CAPITAL_BLOCK_TRIGGERS,
        capital_block_trigger,
    )
    legacy = {
        "unclassifiable_commitment": "unclassifiable_commitment",
        "offer_amount_conflict": "offer_amount_mismatch",
        **dict.fromkeys((
            "offer_provenance_conflict", "offer_attempt_conflict", "attempt_decision_conflict",
            "attempt_amount_conflict", "attempt_projection_conflict", "attempt_intent_conflict",
            "attempt_intent_scope_conflict", "attempt_outcome_evidence_conflict",
            "attempt_outcome_evidence_identity", "attempt_outcome_evidence_scope",
            "duplicate_attempt_intent", "execution_unknown_resolution_conflict",
            "snapshot_conflicting_identity",
        ), "identity_conflict"),
    }
    ledger = {"attempt_evidence_conflict": "identity_conflict",
              "uncertainty_scope_conflict": "identity_conflict",
              "venue_lent_above_ledger": "venue_lent_above_ledger"}
    assert legacy | ledger == CAPITAL_BLOCK_TRIGGERS
    for reason, trigger in (legacy | ledger).items():
        assert capital_block_trigger(reason) == trigger
    assert {"attempt_tail_unbounded"} == CAPITAL_BLOCK_TRANSIENT
    for reason in (*CAPITAL_BLOCK_TRANSIENT, "snapshot_stale", "not_a_reason"):
        assert capital_block_trigger(reason) is None
    assert set(CAPITAL_BLOCK_TRIGGERS.values()) <= TRIGGERS


# ------------------------------------------- automatic resume (ADR 2026-09-26)

MIN_HALT = 15 * 60 * 1000


class Clock:
    def __init__(self, now: int = 0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class RuleState:
    """A trading state that applies the real transition rules, in memory."""

    def __init__(self, clock: Clock) -> None:
        from bfx_funding_bot.modules.execution.safety.trading_state import (
            restates,
            validate_transition,
        )
        self._restates, self._validate = restates, validate_transition
        self._clock = clock
        self.rows: list[TradingState] = []

    async def current(self) -> TradingState | None:
        return self.rows[-1] if self.rows else None

    async def transition(self, state: str, *, cause: str, actor: str, reason: str,
                         now_ms: int | None = None) -> TransitionResult:
        now = self._clock() if now_ms is None else now_ms
        current = self.rows[-1] if self.rows else None
        if self._restates(current, state=state, cause=cause):
            assert current is not None
            return TransitionResult(state=current, changed=False, previous=current)
        resumes = sum(1 for row in self.rows if row.state == "ACTIVE" and row.cause == "auto"
                      and row.created_at_ms > now - 24 * 60 * 60 * 1000)
        self._validate(current, state=state, cause=cause, actor=actor, reason=reason,
                       now_ms=now, auto_resumes_in_window=resumes)
        row = TradingState(id=len(self.rows) + 1, state=state, cause=cause, actor=actor,
                           reason=reason, created_at_ms=now)
        self.rows.append(row)
        return TransitionResult(state=row, changed=True, previous=current)


async def _halted_auto(clock: Clock) -> tuple[AutomaticProtection, RuleState]:
    state = RuleState(clock)
    await state.transition("ACTIVE", cause="operator", actor="will", reason="start")
    protection = AutomaticProtection(clock=clock)
    protection.bind(state)
    protection.trip(VENUE_LENT_ABOVE_LEDGER, "unexplained=1")
    await protection.run_pending()
    assert (await state.current()).state == "HALTED"
    return protection, state


def _clean(protection: AutomaticProtection, clock: Clock, n: int, *, step: int = 60_000,
           first: int = 100) -> None:
    for seq in range(n):
        clock.now += step
        protection.observe_clean(str(first + seq))


async def test_a_cleared_automatic_halt_resumes_after_three_clean_snapshots_and_15_min() -> None:
    clock = Clock(1_000)
    protection, state = await _halted_auto(clock)
    _clean(protection, clock, 3)
    assert not await protection.resume_if_cleared()  # 3 clean, but only 3 minutes
    clock.now = state.rows[-1].created_at_ms + MIN_HALT - 1
    assert not await protection.resume_if_cleared()
    clock.now += 1
    assert await protection.resume_if_cleared()
    resumed = await state.current()
    assert (resumed.state, resumed.cause, resumed.actor) == ("ACTIVE", "auto", "auto-resume")
    assert "clean observations: 100, 101, 102;" in resumed.reason and "venue_lent_above_ledger" in resumed.reason


async def test_one_observation_reported_again_is_not_a_second_clean_observation() -> None:
    clock = Clock(1_000)
    protection, state = await _halted_auto(clock)
    clock.now += 60_000
    protection.observe_clean("ledger:v1:obs:a")
    for _ in range(3):
        clock.now += 60_000
        protection.observe_clean("ledger:v1:obs:a")
    protection.observe_clean("ledger:v1:obs:b")
    clock.now = state.rows[-1].created_at_ms + MIN_HALT
    assert not await protection.resume_if_cleared()  # two distinct observations
    protection.observe_clean("ledger:v1:obs:c")
    assert await protection.resume_if_cleared()


async def test_observations_without_a_reference_each_count() -> None:
    clock = Clock(1_000)
    protection, state = await _halted_auto(clock)
    for _ in range(3):
        clock.now += 60_000
        protection.observe_clean(None)
    clock.now = state.rows[-1].created_at_ms + MIN_HALT
    assert await protection.resume_if_cleared()


async def test_fewer_than_three_clean_snapshots_keep_the_halt() -> None:
    clock = Clock(1_000)
    protection, state = await _halted_auto(clock)
    _clean(protection, clock, 2)
    clock.now += MIN_HALT
    assert not await protection.resume_if_cleared()
    assert (await state.current()).state == "HALTED"


async def test_any_trip_restarts_the_clean_count() -> None:
    clock = Clock(1_000)
    protection, _state = await _halted_auto(clock)
    _clean(protection, clock, 2)
    protection.trip(COMMAND_RATE_EXCEEDED, "still throttling")  # condition persists
    await protection.run_pending()
    _clean(protection, clock, 2, first=200)
    clock.now += MIN_HALT
    assert not await protection.resume_if_cleared()
    _clean(protection, clock, 1, first=300)
    assert await protection.resume_if_cleared()


async def test_clean_snapshots_from_before_the_halt_do_not_count() -> None:
    clock = Clock(1_000)
    state = RuleState(clock)
    await state.transition("ACTIVE", cause="operator", actor="will", reason="start")
    protection = AutomaticProtection(clock=clock)
    protection.bind(state)
    _clean(protection, clock, 3)
    # A halt the process did not trip (written before a restart, say): nothing clears it.
    clock.now += 1
    await state.transition("HALTED", cause="auto", actor="auto:x", reason="x")
    clock.now += MIN_HALT
    assert not await protection.resume_if_cleared()


async def test_an_operator_halt_is_never_resumed_automatically() -> None:
    clock = Clock(1_000)
    protection, state = await _halted_auto(clock)
    clock.now += 1
    await state.transition("HALTED", cause="operator", actor="will", reason="kill")
    _clean(protection, clock, 3)
    clock.now += MIN_HALT
    assert not await protection.resume_if_cleared()
    assert (await state.current()).cause == "operator"


async def test_the_third_halt_in_a_day_stays_and_alerts_once(sent) -> None:
    clock = Clock(1_000)
    protection, state = await _halted_auto(clock)
    for _ in range(2):
        _clean(protection, clock, 3)
        clock.now += MIN_HALT
        assert await protection.resume_if_cleared()
        protection.trip(VENUE_LENT_ABOVE_LEDGER, "again")
        await protection.run_pending()
    _clean(protection, clock, 3)
    clock.now += MIN_HALT
    assert not await protection.resume_if_cleared()
    _clean(protection, clock, 1)
    assert not await protection.resume_if_cleared()
    assert (await state.current()).state == "HALTED"
    limits = [fields for event, fields in sent if event == "auto_resume_limit_reached"]
    assert len(limits) == 1 and limits[0]["state_id"] == state.rows[-1].id
    assert protection.auto_resumes == 2


async def test_the_supervised_task_resumes_on_a_clean_observation() -> None:
    clock = Clock(1_000)
    protection, state = await _halted_auto(clock)
    stop = asyncio.Event()
    task = asyncio.create_task(protection.run(stop))
    clock.now += MIN_HALT
    _clean(protection, clock, 3)
    for _ in range(200):
        if (await state.current()).state == "ACTIVE":
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert (await state.current()).state == "ACTIVE"
