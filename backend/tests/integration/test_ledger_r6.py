"""R6 quarantine conversions, atomicity and bounded evidence on migrated PostgreSQL.

Mutations 5-6: margin test and the pure test_r6 completeness cases.
Mutation 7: test_r6_source_attempt_unique_fk_and_nulls in test_ledger_schema_roles.py.
Parity with the unresolved-ACK case test_ledger_basis.py:638-646 remains there.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.ledger import OfferHistory, Quarantine
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisQuarantineRow,
    CapitalCommandClockRow,
    QuarantineOpeningRow,
)

from .test_ledger_basis import (
    JOURNAL,
    SCOPE,
    _observation,
    _offer,
    _trade,
    ledger_db,  # noqa: F401 - fixture dependency
)
from .test_ledger_basis import (
    ledger as ledger_fixture,  # noqa: F401 - fixture re-export
)

pytestmark = pytest.mark.integration


def _evidence(*, start: int = 40_000, end: int = 100_000, **kwargs):
    observation = _observation(**kwargs)
    return replace(
        observation,
        coverage=replace(
            observation.coverage,
            history_requested_start_ms=start,
            history_requested_end_ms=end,
            trades_requested_start_ms=40_000,
            trades_requested_end_ms=100_000,
        ),
    )


async def _openings(book) -> list[QuarantineOpeningRow]:
    async with book.factory.begin() as session:
        return list(await session.scalars(select(QuarantineOpeningRow)))


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (40_000, 100_000, "quarantined"),
        (40_001, 100_000, "unresolved"),
        (100_000, 100_000, "unresolved"),
        (40_000, 99_999, "unresolved"),
    ],
)
@pytest.mark.asyncio
async def test_r6_margin_and_end_boundaries(
    ledger_fixture,  # noqa: F811 - fixture parameter
    start: int,
    end: int,
    expected: str,
) -> None:
    await ledger_fixture.accept(_observation())
    attempt_id = await ledger_fixture.attempt(
        amount="200", venue_offer_id="gone", started_at_ms=100_000
    )
    basis = await ledger_fixture.accept(_evidence(start=start, end=end), started_at_ms=200_000)
    assert basis.attempts[attempt_id] == ("fUST", expected)
    openings = await _openings(ledger_fixture)
    if expected == "quarantined":
        assert not basis.blocked
        assert len(openings) == 1
        opening = openings[0]
        assert opening.source_attempt_id == attempt_id
        assert opening.intended_amount == Decimal("200")
        assert opening.opened_revision > basis.row.accept_revision
        async with ledger_fixture.factory.begin() as session:
            assert (
                await session.scalar(
                    select(AcceptedCapitalBasisQuarantineRow.quarantine_id).where(
                        AcceptedCapitalBasisQuarantineRow.basis_id == basis.row.id
                    )
                )
                == opening.quarantine_id
            )
    else:
        assert basis.reasons() == ["unclassifiable_commitment"]
        assert openings == []


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (40_000, 100_000, "quarantined"),
        (40_001, 100_000, "unresolved"),
        (100_000, 100_000, "unresolved"),
        (40_000, 99_999, "unresolved"),
    ],
)
@pytest.mark.asyncio
async def test_r6_trades_range_boundaries(ledger_fixture, start, end, expected) -> None:  # noqa: F811
    await ledger_fixture.accept(_observation())
    attempt = await ledger_fixture.attempt(
        amount="200", venue_offer_id="gone", started_at_ms=100_000
    )
    evidence = _evidence()
    evidence = replace(
        evidence,
        coverage=replace(
            evidence.coverage,
            trades_requested_start_ms=start,
            trades_requested_end_ms=end,
        ),
    )
    basis = await ledger_fixture.accept(evidence, started_at_ms=200_000)
    assert basis.attempts[attempt] == ("fUST", expected)
    assert len(await _openings(ledger_fixture)) == (1 if expected == "quarantined" else 0)
    if expected == "unresolved":
        assert basis.reasons() == ["unclassifiable_commitment"]


@pytest.mark.parametrize("source", ["ack", "bound"])
@pytest.mark.asyncio
async def test_r6_ack_and_bound_convert_once_across_acceptances(
    ledger_fixture,  # noqa: F811 - fixture parameter
    source: str,
) -> None:
    await ledger_fixture.accept(_observation())
    attempt_id = await ledger_fixture.attempt(
        amount="200",
        outcome="ack" if source == "ack" else "unknown",
        venue_offer_id="gone",
        started_at_ms=100_000,
    )
    if source == "bound":
        await ledger_fixture.accept(_evidence(start=100_000), started_at_ms=200_000)
        await ledger_fixture.resolve(attempt_id, "bound_to_venue", "gone")
    first = await ledger_fixture.accept(_evidence(), started_at_ms=300_000)
    assert first.attempts[attempt_id] == ("fUST", "quarantined")
    opening = (await _openings(ledger_fixture))[0]
    for started in (400_000, 500_000):
        next_basis = await ledger_fixture.accept(_evidence(), started_at_ms=started)
        assert not next_basis.blocked
        assert await _openings(ledger_fixture)  # remains open; 3c3 owns automatic resolution
        async with ledger_fixture.factory.begin() as session:
            listed = list(
                await session.scalars(
                    select(AcceptedCapitalBasisQuarantineRow).where(
                        AcceptedCapitalBasisQuarantineRow.basis_id == next_basis.row.id
                    )
                )
            )
            assert [row.quarantine_id for row in listed] == [opening.quarantine_id]
    assert len(await _openings(ledger_fixture)) == 1


@pytest.mark.asyncio
async def test_r6_reuses_existing_source_opening_without_clock_bump(ledger_fixture) -> None:  # noqa: F811
    await ledger_fixture.accept(_observation())
    attempt_id = await ledger_fixture.attempt(
        amount="200", venue_offer_id="gone", started_at_ms=100_000
    )
    opening_id = uuid4()
    async with ledger_fixture.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session,
            SCOPE,
            Quarantine(
                opening_id, "fUST", Decimal("200"), 150_000, {}, source_attempt_id=attempt_id
            ),
        )
        before = await session.scalar(select(CapitalCommandClockRow.revision))
    basis = await ledger_fixture.accept(_evidence(), started_at_ms=200_000)
    assert basis.attempts[attempt_id] == ("fUST", "quarantined")
    assert [row.quarantine_id for row in await _openings(ledger_fixture)] == [opening_id]
    async with ledger_fixture.factory.begin() as session:
        assert await session.scalar(select(CapitalCommandClockRow.revision)) == before


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("active", "reflected"),
        ("active_wrong_amount", "unresolved"),
        ("terminal", "reflected"),
        ("terminal_wrong_amount", "unresolved"),
        ("terminal_wrong_symbol", "unresolved"),
        ("trade_without_live_credit", "unresolved"),
    ],
)
@pytest.mark.asyncio
async def test_r6_existing_identity_is_never_absence(
    ledger_fixture,  # noqa: F811 - fixture parameter
    evidence: str,
    expected: str,
) -> None:
    await ledger_fixture.accept(_observation())
    attempt_id = await ledger_fixture.attempt(
        amount="200", venue_offer_id="gone", started_at_ms=100_000
    )
    kwargs = {}
    if evidence.startswith("active"):
        kwargs["offers"] = (_offer("gone", "300" if evidence.endswith("amount") else "200"),)
    elif evidence.startswith("terminal"):
        kwargs["history"] = (
            OfferHistory(
                _offer(
                    "gone",
                    "300" if evidence.endswith("amount") else "200",
                    "0",
                    symbol="fUSD" if evidence.endswith("symbol") else "fUST",
                ),
                "canceled",
                100_000,
            ),
        )
    else:
        kwargs["trades"] = (_trade("gone", "200"),)
    basis = await ledger_fixture.accept(_evidence(**kwargs), started_at_ms=200_000)
    assert basis.attempts[attempt_id] == ("fUST", expected)
    assert await _openings(ledger_fixture) == []
    if expected == "unresolved":
        assert "unclassifiable_commitment" in basis.reasons()


@pytest.mark.asyncio
async def test_r6_failure_rolls_back_opening_basis_and_clock(ledger_fixture, monkeypatch) -> None:  # noqa: F811
    from bfx_funding_bot.modules.ledger._internal import observation

    await ledger_fixture.accept(_observation())
    await ledger_fixture.attempt(amount="200", venue_offer_id="gone", started_at_ms=100_000)
    async with ledger_fixture.factory.begin() as session:
        clock = await session.scalar(select(CapitalCommandClockRow.revision))
    original = observation.write_basis

    async def fail_after_basis(session, scope, observation_id: UUID):
        await original(session, scope, observation_id)
        raise RuntimeError("crash before acceptance commit")

    monkeypatch.setattr(observation, "write_basis", fail_after_basis)
    with pytest.raises(RuntimeError, match="crash before acceptance commit"):
        await ledger_fixture.accept(_evidence(), started_at_ms=200_000)
    assert await _openings(ledger_fixture) == []
    async with ledger_fixture.factory.begin() as session:
        assert await session.scalar(select(CapitalCommandClockRow.revision)) == clock
