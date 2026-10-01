"""S1-3c3b P1/P2 regressions on migrated PostgreSQL (not run without Docker)."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.ledger import ObservationWindow, OfferHistory, Quarantine
from bfx_funding_bot.modules.ledger.tables import QuarantineMemberRow
from bfx_funding_bot.modules.ledger.wiring import build_ledger_observations

from .test_ledger_basis import (
    JOURNAL,
    SCOPE,
    _observation,
    _offer,
    ledger_db,  # noqa: F401 - fixture dependency
)
from .test_ledger_basis import ledger as ledger_fixture  # noqa: F401 - fixture re-export
from .test_ledger_r6 import _evidence, _openings

pytestmark = pytest.mark.integration


async def _r6(book):
    await book.accept(_observation(), started_at_ms=150_000)
    attempt = await book.attempt(amount="200", venue_offer_id="gone", started_at_ms=100_000)
    basis = await book.accept(_evidence(), started_at_ms=300_000)
    assert basis.attempts[attempt] == ("fUST", "quarantined")
    opening = (await _openings(book))[0]
    assert opening.source_attempt_id == attempt
    return opening


@pytest.mark.asyncio
async def test_window_covers_unresolved_r6_source_start_minus_margin(ledger_fixture):  # noqa: F811
    opening = await _r6(ledger_fixture)
    # A later basis no longer lists the source attempt as unresolved.
    await ledger_fixture.accept(_evidence(), started_at_ms=500_000)
    async with ledger_fixture.factory.begin() as session:
        window = await build_ledger_observations().observation_window(session, SCOPE)
        assert window == ObservationWindow(100_000, 500_000, 40_000)
    assert opening.source_attempt_id is not None


@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.asyncio
async def test_terminal_reappearance_uses_plain_quarantine_never_r6(ledger_fixture, plain):  # noqa: F811
    r6 = await _r6(ledger_fixture)
    normal_id = uuid4()
    if plain:
        async with ledger_fixture.factory.begin() as session:
            await JOURNAL.open_quarantine(session, SCOPE, Quarantine(
                normal_id, "fUST", Decimal("10"), 310_000, {"reason": "plain"},
            ))
    returned = _offer("returned", "10")
    await ledger_fixture.accept(_evidence(offers=(returned,)), started_at_ms=400_000)
    await ledger_fixture.accept(
        _evidence(history=(OfferHistory(returned, "canceled", 101_001),)),
        started_at_ms=500_000,
    )
    await ledger_fixture.accept(_evidence(offers=(returned,)), started_at_ms=600_000)
    openings = await _openings(ledger_fixture)
    assert len(openings) == 2
    async with ledger_fixture.factory.begin() as session:
        member = await session.scalar(select(QuarantineMemberRow).where(
            QuarantineMemberRow.venue_object_id == "returned",
        ))
        assert member is not None and member.quarantine_id != r6.quarantine_id
        if plain:
            assert member.quarantine_id == normal_id
        else:
            new = next(row for row in openings if row.quarantine_id == member.quarantine_id)
            assert new.source_attempt_id is None
    # Repeated acceptance must neither duplicate nor move membership into R6.
    await ledger_fixture.accept(_evidence(offers=(returned,)), started_at_ms=700_000)
    assert len(await _openings(ledger_fixture)) == 2
    async with ledger_fixture.factory.begin() as session:
        members = list(await session.scalars(select(QuarantineMemberRow)))
        assert len(members) == 1 and members[0].quarantine_id != r6.quarantine_id
