"""S1-3d0 windows and credit trade coverage on migrated PostgreSQL."""

from dataclasses import replace

import pytest
from sqlalchemy import event

from bfx_funding_bot.modules.ledger import ObservationWindow, OfferHistory, Scope
from bfx_funding_bot.modules.ledger._internal import reads
from bfx_funding_bot.modules.ledger.wiring import build_ledger_observations

from .test_ledger_basis import (
    JOURNAL,
    SCOPE,
    _credit,
    _ledger_mappers,
    _observation,
    _offer,
    _trade,
    ledger_db,  # noqa: F401 - fixture dependency
)
from .test_ledger_basis import (
    ledger as _ledger_fixture,
)

pytestmark = pytest.mark.integration
OBSERVATIONS = build_ledger_observations()
ledger_fixture = _ledger_fixture


async def _window(book, scope=SCOPE) -> ObservationWindow:
    async with book.factory.begin() as session:
        return await OBSERVATIONS.observation_window(session, scope)


@pytest.mark.asyncio
async def test_window_without_anchors_returns_none(ledger_fixture) -> None:
    assert await _window(ledger_fixture) == ObservationWindow(None, None, None)


@pytest.mark.asyncio
async def test_window_previous_accepted_query_ignores_newer_pending_and_fenced(ledger_fixture) -> None:
    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    expected = ObservationWindow(None, 150_000, 90_000)
    assert await _window(ledger_fixture) == expected
    async with ledger_fixture.factory.begin() as session:
        pending = await OBSERVATIONS.begin_query(session, SCOPE, 200_000)
    assert await _window(ledger_fixture) == expected
    async with ledger_fixture.factory.begin() as session:
        await JOURNAL.bump_clock(session, SCOPE)
        result = await OBSERVATIONS.accept(
            session,
            SCOPE,
            pending,
            replace(_observation(), finished_at_ms=200_001),
            replace(_observation(), finished_at_ms=200_003),
            200_002,
        )
        assert result.decision == "fenced"
    assert await _window(ledger_fixture) == expected
    assert await _window(
        ledger_fixture, Scope(SCOPE.exchange_account_id, "other")
    ) == ObservationWindow(None, None, None)


@pytest.mark.asyncio
async def test_window_high_water_tail_uses_earliest_start_not_sequence(ledger_fixture) -> None:
    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=20_000)
    await ledger_fixture.accept(_observation(), started_at_ms=300_000)
    # Each helper call creates its own execution_decisions row (UNIQUE FK).
    await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=250_000)
    await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=200_000)
    assert await _window(ledger_fixture) == ObservationWindow(200_000, 300_000, 140_000)


@pytest.mark.asyncio
async def test_window_basis_unresolved_survives_the_high_water(ledger_fixture) -> None:
    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    attempt = await ledger_fixture.attempt(
        amount="10", venue_offer_id="gone", started_at_ms=100_000
    )
    basis = await ledger_fixture.accept(_observation(), started_at_ms=300_000)
    assert basis.attempts[attempt] == ("fUST", "unresolved")
    await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=200_000)
    assert await _window(ledger_fixture) == ObservationWindow(100_000, 300_000, 40_000)


@pytest.mark.asyncio
async def test_window_open_unknown_until_resolution_is_accepted(ledger_fixture) -> None:
    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    unknown = await ledger_fixture.attempt(
        amount="10", outcome="unknown", started_at_ms=100_000
    )
    assert await _window(ledger_fixture) == ObservationWindow(100_000, 150_000, 40_000)
    basis = await ledger_fixture.accept(_observation(), started_at_ms=300_000)
    assert basis.attempts[unknown] == ("fUST", "unresolved")
    assert await _window(ledger_fixture) == ObservationWindow(100_000, 300_000, 40_000)
    await ledger_fixture.resolve(unknown, "not_accepted")
    # The latest basis still lists it; the next acceptance removes that anchor.
    assert await _window(ledger_fixture) == ObservationWindow(100_000, 300_000, 40_000)
    await ledger_fixture.accept(_observation(), started_at_ms=500_000)
    assert await _window(ledger_fixture) == ObservationWindow(None, 500_000, 440_000)


@pytest.mark.asyncio
async def test_window_previous_query_can_precede_all_attempts(ledger_fixture) -> None:
    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=200_000)
    assert await _window(ledger_fixture) == ObservationWindow(200_000, 150_000, 90_000)


@pytest.mark.asyncio
async def test_window_read_work_does_not_grow_with_settled_history(ledger_fixture) -> None:
    classes = {mapper.class_ for mapper in _ledger_mappers()}

    async def history(n):
        for _ in range(n):
            await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=100_000)
            await ledger_fixture.accept(_observation(), started_at_ms=150_000)

    async def measured():
        loads = 0

        def loaded(_target, _context):
            nonlocal loads
            loads += 1

        for cls in classes:
            event.listen(cls, "load", loaded)
        try:
            assert await _window(ledger_fixture) == ObservationWindow(None, 150_000, 90_000)
        finally:
            for cls in classes:
                event.remove(cls, "load", loaded)
        return loads

    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    await history(2)
    small = await measured()
    await history(25)
    assert await measured() == small > 0


@pytest.mark.asyncio
async def test_window_tail_cap_fails_closed(ledger_fixture, monkeypatch) -> None:
    from bfx_funding_bot.modules.ledger import LedgerReadUnbounded

    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    monkeypatch.setattr(reads, "MAX_TAIL_ATTEMPTS", 1)
    await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=100_000)
    assert await _window(ledger_fixture) == ObservationWindow(100_000, 150_000, 40_000)
    await ledger_fixture.attempt(amount="10", outcome="rejected", started_at_ms=200_000)
    with pytest.raises(LedgerReadUnbounded):
        await _window(ledger_fixture)


@pytest.mark.parametrize("kind", ["credit", "loan"])
@pytest.mark.parametrize(
    ("start", "end", "blocked"),
    [(40_000, 100_000, False), (40_001, 100_000, True), (40_000, 99_999, True)],
)
@pytest.mark.asyncio
async def test_credit_opening_requires_trades_margin_and_end(
    ledger_fixture, kind, start, end, blocked
) -> None:
    await ledger_fixture.accept(_observation(usd=True), started_at_ms=150_000)
    await ledger_fixture.attempt(amount="200", venue_offer_id="ours", started_at_ms=100_000)
    evidence = _observation(
        "800",
        usd=True,
        credits=(_credit("c1", "200", kind=kind, opening=100_000),),
        # A fully filled ack is in terminal history (as Bitfinex reports it).
        history=(OfferHistory(_offer("ours", "200", "0", created=100_000), "executed", 1150),),
        trades=(_trade("ours", "200", mts_create=100_000),),
    )
    evidence = replace(
        evidence,
        coverage=replace(
            evidence.coverage,
            trades_requested_start_ms=start,
            trades_requested_end_ms=end,
        ),
    )
    basis = await ledger_fixture.accept(evidence, started_at_ms=200_000)
    assert (basis.symbols["fUST"].block is not None) is blocked
    assert basis.symbols["fUSD"].block is None
    assert basis.row.scope_block is None
    assert basis.symbols["fUST"].credits == 200
    if blocked:
        assert "trades_range_uncovered" in basis.reasons()


@pytest.mark.asyncio
async def test_carried_credit_needs_no_trades_coverage(ledger_fixture) -> None:
    """Carry attributes by the previous basis's (symbol, period, opening); old
    credits must not demand trades back to their opening on every observation."""
    await ledger_fixture.accept(_observation(), started_at_ms=150_000)
    await ledger_fixture.attempt(amount="200", venue_offer_id="ours", started_at_ms=100_000)
    await ledger_fixture.accept(
        _observation("800", offers=(_offer("ours", "200", created=100_000),)),
        started_at_ms=200_000,
    )
    evidence = _observation("800", credits=(_credit("c1", "200", opening=100_000),))
    basis = await ledger_fixture.accept(evidence, started_at_ms=300_000)
    assert basis.credits["c1"] == ("recent_fill", ["a30"])
    evidence = replace(
        evidence, coverage=replace(evidence.coverage, trades_requested_start_ms=40_001)
    )
    basis = await ledger_fixture.accept(evidence, started_at_ms=400_000)
    assert basis.credits["c1"] == ("carry", ["a30"])
    assert basis.reasons() == []
    assert basis.symbols["fUST"].block is None
