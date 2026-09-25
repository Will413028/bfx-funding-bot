"""Kill switch against real SQL (SQLite and PostgreSQL); only the venue is fake.

The fake venue holds live offers per currency and records, for every
cancel-all it receives, the trading state as another reader sees it at that
moment -- so "HALTED is written first" is observed, not assumed.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.events import (
    SnapshotCoverage,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.protocols import FundingCancelAllResult
from bfx_funding_bot.modules.execution.safety.kill_switch import KillSwitch
from bfx_funding_bot.modules.execution.safety.tables import (
    FundingCancelAllAuditRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import read_current
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow

from .test_capital_command_boundary import append_cancel_race_unknown, boundary

pytestmark = pytest.mark.integration


@pytest.fixture
def capital_db(migrated_db):
    """The migrated PostgreSQL schema: its triggers are the rules' authority."""
    return migrated_db


class FakeVenue:
    def __init__(self, factory, account, offers: dict[str, set[str]]) -> None:
        self.factory, self.account, self.offers = factory, account, offers
        self.calls: list[tuple[str, str | None]] = []
        self.fail: set[str] = set()

    async def cancel_all_funding_offers(self, *, currency, ctx):
        async with self.factory() as observer:
            state = await read_current(observer, account_id=self.account, environment="ci")
        self.calls.append((currency, state.state if state is not None else None))
        if currency in self.fail:
            raise ExecutorTransientError(f"venue unavailable for {currency}")
        self.offers.pop(currency, None)
        return FundingCancelAllResult(outcome="acknowledged", venue_status="SUCCESS",
                                      text="Cancelled all funding offers")


class Lock:
    def __init__(self, held: bool = True) -> None:
        self.held = held

    async def verify_held(self) -> bool:
        return self.held


async def exposed_account(factory, account):
    """A managed fUST offer, an orphan fUSD offer, and an UNKNOWN fBTC submit."""
    gate, _, ready, ctx, runtime, trading = await boundary(factory, account)
    await gate.submit(ready, ctx)  # managed offer 101 (fUST)
    offers = (
        VenueOfferObservation("101", "fUST", Decimal("500"), Decimal("500"), Decimal("0.0001"), 2,
                              "ACTIVE", 1100, 1100),
        VenueOfferObservation("555", "fUSD", Decimal("40"), Decimal("40"), Decimal("0.0002"), 2,
                              "ACTIVE", 1000, 1000),
    )
    # Capital acceptance refuses a snapshot with an orphan in it; recovery then
    # records the raw observation through the event writer, as done here.
    observed = VenueSnapshotObserved(
        account_id=str(account), environment="ci", query_started_at_ms=1000,
        query_finished_at_ms=1050, offers=offers, credits=(),
        wallet_available={"fUST": Decimal("500"), "fUSD": Decimal("0")},
        coverage=SnapshotCoverage(True, True, True),
    )
    async with factory.begin() as session:
        seq = (await runtime.repository.writer.append(session, observed)).event_seq
        await runtime.repository.writer.append(session, VenueOfferQuarantined(
            venue_offer_id="555", symbol="fUSD", amount=Decimal("40"), account_id=str(account),
            observed_at_ms=1060, event_seq=seq,
        ))
    await append_cancel_race_unknown(factory, account, symbol="fBTC")
    async with factory() as session:
        open_scopes = set(await session.scalars(select(ExecutionUncertaintyRow.kind + ":"
            + ExecutionUncertaintyRow.symbol).where(ExecutionUncertaintyRow.state == "open")))
    assert open_scopes == {"unattributed_venue_offer:fUSD", "submit_outcome_unknown:fBTC"}
    venue = FakeVenue(factory, account, {"UST": {"101"}, "USD": {"555"}, "BTC": {"777"}})
    return gate, ctx, trading, venue


def kill_switch(factory, trading, ctx, venue, lock=None, **kwargs):
    return KillSwitch(trading_state=trading, session_factory=factory, ctx=ctx,
                      configured_symbols={"fUST"}, venue=venue,
                      writer_lock=lock if lock is not None else Lock(), clock=lambda: 5000, **kwargs)


async def audit(factory):
    async with factory() as session:
        rows = (await session.scalars(select(FundingCancelAllAuditRow).order_by(
            FundingCancelAllAuditRow.id))).all()
    return [(row.currency, row.phase, row.trading_state_id) for row in rows]


async def halted_rows(factory):
    async with factory() as session:
        return (await session.scalars(select(TradingStateRow).where(
            TradingStateRow.state == "HALTED"))).all()


@pytest.mark.asyncio
async def test_kill_writes_halted_then_cancels_managed_orphan_and_unknown_currencies(capital_db):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)
    result = await kill_switch(factory, trading, ctx, venue).engage(
        cause="operator", actor="test", reason="stop everything")

    assert result.complete
    assert (result.state.state, result.state.cause) == ("HALTED", "operator")
    # Every call saw HALTED already committed; orphan (USD) and UNKNOWN (BTC)
    # currencies are cancelled although nothing is configured to trade them.
    assert venue.calls == [("BTC", "HALTED"), ("USD", "HALTED"), ("UST", "HALTED")]
    assert venue.offers == {}
    halted_id = result.state.id
    assert await audit(factory) == [
        (currency, phase, halted_id)
        for currency in ("BTC", "USD", "UST") for phase in ("requested", "acknowledged")
    ]


@pytest.mark.asyncio
async def test_venue_failure_keeps_halted_and_a_retry_completes_without_a_second_halt(capital_db):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)
    venue.fail = {"USD"}
    switch = kill_switch(factory, trading, ctx, venue)

    first = await switch.engage(cause="operator", actor="will", reason="kill")
    assert not first.complete
    assert {o.currency: o.phase for o in first.cancel_all} == {
        "BTC": "acknowledged", "USD": "failed", "UST": "acknowledged"}
    failed = next(o for o in first.cancel_all if o.currency == "USD")
    assert "ExecutorTransientError" in (failed.detail or "")
    assert (await trading.current()).state == "HALTED"
    assert venue.offers == {"USD": {"555"}}

    venue.fail = set()
    second = await switch.engage(cause="operator", actor="will", reason="retry kill")
    assert second.complete
    assert not second.state_changed and second.state.id == first.state.id
    assert venue.offers == {}
    assert len(await halted_rows(factory)) == 1
    usd = [phase for currency, phase, _ in await audit(factory) if currency == "USD"]
    assert usd == ["requested", "failed", "requested", "acknowledged"]


@pytest.mark.asyncio
async def test_no_venue_call_when_halted_cannot_be_written(capital_db, monkeypatch):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)

    async def unwritable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(trading, "transition", unwritable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await kill_switch(factory, trading, ctx, venue).engage(
            cause="operator", actor="test", reason="stop")
    assert venue.calls == []
    assert await audit(factory) == []


@pytest.mark.asyncio
async def test_without_the_writer_lock_the_stop_is_recorded_but_the_venue_untouched(capital_db):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)
    result = await kill_switch(factory, trading, ctx, venue, lock=Lock(held=False)).engage(
        cause="operator", actor="will", reason="kill")
    assert result.state.state == "HALTED"
    assert not result.complete
    assert {(o.phase, o.detail) for o in result.cancel_all} == {("skipped", "writer_lock_not_held")}
    assert venue.calls == []
    assert {phase for _, phase, _ in await audit(factory)} == {"skipped"}


@pytest.mark.asyncio
async def test_kill_waits_for_an_in_flight_command_after_writing_halted(capital_db):
    """A submit already past its transport recheck must not land after the
    cancel-all: the kill holds the account command lock for the venue part.

    Synchronised on events, not elapsed time: the test learns that the kill has
    written HALTED and reached the quiesce step from the quiesce hook itself, so
    a slow machine only makes it slower, never red. The generous bounds below are
    functional (a hang fails the test), not timing contracts; the production
    quiesce limit is untouched and exercised by the wedged-command test.
    """
    factory, account = capital_db
    gate, ctx, trading, venue = await exposed_account(factory, account)
    waiting_for_commands = asyncio.Event()

    @contextlib.asynccontextmanager
    async def observed_quiesce():
        waiting_for_commands.set()
        async with gate.quiesced(str(account), timeout_s=300) as quiet:
            yield quiet

    switch = kill_switch(factory, trading, ctx, venue, quiesce=observed_quiesce)
    in_flight = gate._account_locks.setdefault((str(account), "ci"), asyncio.Lock())
    await in_flight.acquire()
    task = asyncio.create_task(switch.engage(cause="operator", actor="will", reason="kill"))
    try:
        await asyncio.wait_for(waiting_for_commands.wait(), timeout=60)
        # HALTED is committed before the kill waits for the in-flight command...
        assert (await trading.current()).state == "HALTED"
        # ...and while that command holds the lock nothing reaches the venue,
        # however many times the kill task is scheduled.
        for _ in range(20):
            await asyncio.sleep(0)
        assert venue.calls == [] and not task.done()
        in_flight.release()
        result = await asyncio.wait_for(task, timeout=60)
    finally:
        if in_flight.locked():
            in_flight.release()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert result.complete
    assert venue.calls == [("BTC", "HALTED"), ("USD", "HALTED"), ("UST", "HALTED")]


@pytest.mark.asyncio
async def test_a_wedged_command_does_not_hold_the_kill_hostage(capital_db):
    factory, account = capital_db
    gate, ctx, trading, venue = await exposed_account(factory, account)
    switch = kill_switch(factory, trading, ctx, venue,
                         quiesce=lambda: gate.quiesced(str(account), timeout_s=0.05))
    wedged = gate._account_locks.setdefault((str(account), "ci"), asyncio.Lock())
    await wedged.acquire()
    try:
        result = await asyncio.wait_for(
            switch.engage(cause="operator", actor="will", reason="kill"), timeout=10)
    finally:
        wedged.release()
    assert result.complete
    assert all((o.detail or "").startswith("in_flight_command_not_quiesced")
               for o in result.cancel_all)


@pytest.mark.asyncio
async def test_unreadable_scope_still_cancels_configured_currencies(capital_db, monkeypatch):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)
    switch = kill_switch(factory, trading, ctx, venue)
    original = switch._sf

    class Unreadable:
        def __call__(self):
            raise RuntimeError("scope read failed")

        def begin(self):
            return original.begin()

    monkeypatch.setattr(switch, "_sf", Unreadable())
    result = await switch.engage(cause="operator", actor="will", reason="kill")
    assert result.scope_error == "scope_unreadable: RuntimeError"
    assert not result.complete
    assert [currency for currency, _ in venue.calls] == ["UST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["material_deploy", "nonsense"])  # a retired cause, a bogus one
async def test_only_stop_causes_may_engage(capital_db, cause):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)
    before = await trading.current()
    with pytest.raises(ValueError, match="cannot halt"):
        await kill_switch(factory, trading, ctx, venue).engage(cause=cause, actor="t", reason="t")
    assert await trading.current() == before
    assert venue.calls == []


@pytest.mark.asyncio
async def test_credentials_never_reach_the_audit(capital_db):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)
    ctx = replace(ctx, credentials=replace(ctx.credentials, api_key="KEY-123", api_secret="SECRET-9"))
    venue.fail = {"UST"}

    async def echo(*, currency, ctx):
        raise ExecutorTransientError(f"upstream echoed {ctx.credentials.api_key} {ctx.credentials.api_secret}")

    venue.cancel_all_funding_offers = echo
    await kill_switch(factory, trading, ctx, venue).engage(cause="operator", actor="t", reason="t")
    async with factory() as session:
        details = [row.detail or "" for row in (await session.scalars(select(FundingCancelAllAuditRow))).all()]
    assert details and not any("KEY-123" in d or "SECRET-9" in d for d in details)


@pytest.mark.asyncio
async def test_an_automatic_kill_skips_the_venue_when_already_halted_but_an_operator_retry_does_not(capital_db):
    factory, account = capital_db
    _, ctx, trading, venue = await exposed_account(factory, account)
    switch = kill_switch(factory, trading, ctx, venue)
    first = await switch.engage(cause="auto", actor="auto:orphan_quarantined", reason="orphan",
                                when_already_halted="skip")
    assert first.state_changed and first.complete and len(venue.calls) == 3
    persisting = await switch.engage(cause="auto", actor="auto:orphan_quarantined",
                                     reason="orphan again", when_already_halted="skip")
    assert not persisting.state_changed and persisting.cancel_all == ()
    assert len(venue.calls) == 3 and len(await audit(factory)) == 6
    retried = await switch.engage(cause="operator", actor="will", reason="retry the venue part")
    assert not retried.state_changed and len(venue.calls) == 6 and len(await audit(factory)) == 12


# ------------------------------------------------ managed scope (level 3)


class Canceller:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.cancelled: list[str] = []
        self.fail = fail or set()

    async def cancel(self, *, venue_offer_id, signal_correlation_id, account_id, ctx):
        if venue_offer_id in self.fail:
            raise ExecutorTransientError("venue unavailable")
        self.cancelled.append(venue_offer_id)


async def _offer_rows(factory, account):
    """Managed 101 and 102 (a durable intent traces to them), foreign 555 (manual),
    and a terminal managed 103."""
    from bfx_funding_bot.modules.execution.event_store.tables import VenueOfferStateRow
    async with factory.begin() as session:
        for offer_id, symbol, decision, terminal in (
                ("101", "fUST", "d-101", False), ("102", "fUSD", "d-102", False),
                ("555", "fUST", None, False), ("103", "fUST", "d-103", True)):
            session.add(VenueOfferStateRow(
                exchange_account_id=account, deployment_environment="ci", venue_offer_id=offer_id,
                symbol=symbol, amount_original=Decimal("200"), amount_remaining=Decimal("200"),
                rate=Decimal("0.0002"), period_days=2, status="ACTIVE", flags={}, mts_created=1,
                mts_updated=1, first_seen_event_seq=1, last_seen_event_seq=1, is_terminal=terminal,
                execution_decision_id=decision,
                signal_correlation_id="00000000-0000-4000-8000-000000000001" if decision else None))


@pytest.mark.asyncio
async def test_an_automatic_kill_cancels_only_managed_offers_by_id(capital_db):
    """Lending envelope D2/D3: an automatic stop never touches an offer placed by
    hand, and never calls the venue cancel-all."""
    from bfx_funding_bot.modules.execution.managed_cancel import ManagedOfferSweep
    factory, account = capital_db
    _, _, _, ctx, _, trading = await boundary(factory, account)
    await _offer_rows(factory, account)
    canceller = Canceller(fail={"102"})
    venue = FakeVenue(factory, account, {"UST": {"101", "555"}})
    sweep = ManagedOfferSweep(session_factory=factory, account_id=account, environment="ci",
                              canceller=canceller, ctx=ctx)
    result = await kill_switch(factory, trading, ctx, venue, sweep=sweep).engage(
        cause="auto", actor="auto:identity_conflict", reason="conflict",
        when_already_halted="skip", scope="managed")
    assert (result.state.state, result.state.cause) == ("HALTED", "auto")
    assert canceller.cancelled == ["101"]
    assert result.managed is not None and [offer for offer, _ in result.managed.failed] == ["102"]
    assert not result.complete  # a managed cancel did not go out
    assert venue.calls == [] and await audit(factory) == []
    # Persisting: already HALTED, nothing more is cancelled.
    again = await kill_switch(factory, trading, ctx, venue, sweep=sweep).engage(
        cause="auto", actor="auto:identity_conflict", reason="conflict",
        when_already_halted="skip", scope="managed")
    assert not again.state_changed and canceller.cancelled == ["101"]


@pytest.mark.asyncio
async def test_a_managed_kill_without_a_sweep_still_halts_and_says_so(capital_db):
    factory, account = capital_db
    _, _, _, ctx, _, trading = await boundary(factory, account)
    venue = FakeVenue(factory, account, {})
    result = await kill_switch(factory, trading, ctx, venue).engage(
        cause="auto", actor="auto:x", reason="x", scope="managed")
    assert result.state.state == "HALTED"
    assert result.scope_error == "managed_sweep_not_wired" and not result.complete
    assert venue.calls == []
