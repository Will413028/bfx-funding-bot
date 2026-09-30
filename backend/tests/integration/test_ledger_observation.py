"""S1-2b-1 acceptance on a clone PostgreSQL database.

Apply each mutation alone in ``modules/ledger/_internal/observation.py`` (or
``clock.py`` for admission), run this file and ``test_ledger_journal.py`` on a
fresh clone, then revert it before the next mutation:

* Hash input list order: ``test_equal_unordered_and_conflicting_duplicates`` fails.
* Omit loans from the digest: ``test_incomplete_mismatch_and_fences`` fails.
* Persist a mismatched pair: its observation-count assertion fails.
* Mark absence terminal: ``test_absence_and_same_identity_terminal`` fails.
* Skip reappearance quarantine: ``test_terminal_reappearance`` fails.
* Load every mirror row: ``test_mirror_load_is_bounded`` fails.
* Drop the pending-attempt admission check: ``test_begin_query_refuses_pending_attempt``
  in ``test_ledger_journal.py`` fails.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.ledger import (
    Attempt,
    Coverage,
    Credit,
    CreditHistory,
    Observation,
    Offer,
    OfferHistory,
    Scope,
    Wallet,
)
from bfx_funding_bot.modules.ledger._internal.journal import record_attempt
from bfx_funding_bot.modules.ledger.tables import (
    LedgerObservationCreditHistoryRow,
    LedgerObservationCreditRow,
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    LedgerObservationWalletRow,
    QuarantineMemberRow,
    QuarantineOpeningRow,
    VenueCreditMirrorRow,
    VenueOfferMirrorRow,
)
from bfx_funding_bot.modules.ledger.wiring import build_ledger_journal, build_ledger_observations

from .test_ledger_schema_roles import _A, _B, _D2, _P, ledger_db  # noqa: F401 - fixture re-export
from .test_ledger_schema_roles import seeded as seeded_fixture  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
SCOPE = Scope(UUID(_A), "ci")
JOURNAL = build_ledger_journal()
OBSERVATIONS = build_ledger_observations()


def _engine(sync_engine):
    return create_async_engine(
        sync_engine.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg")
    )


def _coverage(*, complete: bool = True) -> Coverage:
    return Coverage(
        complete,
        complete,
        complete,
        complete,
        complete,
        complete,
        1,
        1,
        1,
        1,
        1,
        1,
        trades_complete=complete,
    )


def _offer(venue_id: str = "o1", *, symbol: str = "fUST") -> Offer:
    return Offer(
        venue_id,
        symbol,
        Decimal("2"),
        Decimal("1"),
        None,
        False,
        2,
        None,
        None,
        "active",
        1,
        None,
        {},
    )


def _credit(source_kind: str = "credit", venue_id: str = "c1") -> Credit:
    assert source_kind in ("credit", "loan")
    return Credit(
        source_kind,
        venue_id,
        "fUST",
        Decimal("1"),
        None,
        None,
        "active",
        None,
        None,
        None,
        1,
        {},
    )  # type: ignore[arg-type]


def _observation(
    *,
    offers: tuple[Offer, ...] = (),
    credits: tuple[Credit, ...] = (),
    coverage: Coverage | None = None,
    offer_history: tuple[OfferHistory, ...] = (),
    credit_history: tuple[CreditHistory, ...] = (),
    finished: int = 2,
) -> Observation:
    return Observation(
        (Wallet("funding", "UST", Decimal("3"), Decimal("9"), "fUST"),),
        offers,
        credits,
        coverage or _coverage(),
        finished,
        offer_history,
        credit_history,
    )


async def _begin(factory, started: int = 1):
    async with factory.begin() as session:
        return await JOURNAL.begin_query(session, SCOPE, started)


async def _accept(factory, first: Observation, confirmation: Observation | None = None):
    handle = await _begin(factory)
    async with factory.begin() as session:
        return await OBSERVATIONS.accept(
            session,
            SCOPE,
            handle,
            first,
            confirmation or replace(first, finished_at_ms=4, offer_history=(), credit_history=()),
            3,
        )


@pytest_asyncio.fixture
async def observation_factory(ledger_db):  # noqa: F811
    with ledger_db.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','ledger')"),
            {"id": _A},
        )
    engine = _engine(ledger_db)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_equal_unordered_and_conflicting_duplicates(observation_factory) -> None:
    first = _observation(offers=(_offer("o2"), _offer("o1")), credits=(_credit("loan"), _credit()))
    confirmation = replace(
        first,
        offers=tuple(reversed(first.offers)),
        credits=tuple(reversed(first.credits)),
        coverage=replace(
            first.coverage, offer_history_complete=False, credit_history_complete=False
        ),
        finished_at_ms=4,
    )
    result = await _accept(observation_factory, first, confirmation)
    assert result.decision == "accepted"
    async with observation_factory.begin() as session:
        assert await session.scalar(select(func.count()).select_from(VenueOfferMirrorRow)) == 2
        assert await session.scalar(select(func.count()).select_from(VenueCreditMirrorRow)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(LedgerObservationWalletRow)) == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(LedgerObservationOfferRow)) == 2
        )
        assert (
            await session.scalar(select(func.count()).select_from(LedgerObservationCreditRow)) == 2
        )
        stored = await session.get(LedgerObservationRow, result.observation_id)
        assert stored is not None and stored.evidence["confirmation_started_at_ms"] == 3
    with pytest.raises(ValueError, match="conflicting"):
        await _accept(
            observation_factory, _observation(offers=(_offer(), replace(_offer(), status="OTHER")))
        )


@pytest.mark.asyncio
async def test_incomplete_mismatch_and_fences(observation_factory) -> None:
    incomplete = await _accept(
        observation_factory, _observation(coverage=_coverage(complete=False))
    )
    assert incomplete.decision == "incomplete_or_unequal"
    first = _observation(credits=(_credit("loan"),))
    mismatched = await _accept(
        observation_factory, first, replace(first, credits=(), finished_at_ms=4)
    )
    assert mismatched.decision == "incomplete_or_unequal"
    async with observation_factory.begin() as session:
        assert (
            await session.scalar(select(func.count()).select_from(LedgerObservationQueryRow)) == 2
        )
        assert await session.scalar(select(func.count()).select_from(LedgerObservationRow)) == 0
    old = await _begin(observation_factory)
    await _begin(observation_factory)
    async with observation_factory.begin() as session:
        result = await OBSERVATIONS.accept(
            session, SCOPE, old, first, replace(first, finished_at_ms=4), 3
        )
        assert result.decision == "fenced"
    command_fenced = await _begin(observation_factory)
    async with observation_factory.begin() as session:
        await JOURNAL.bump_clock(session, SCOPE)
    async with observation_factory.begin() as session:
        result = await OBSERVATIONS.accept(
            session, SCOPE, command_fenced, first, replace(first, finished_at_ms=4), 3
        )
        assert result.decision == "fenced"
        assert await session.scalar(select(func.count()).select_from(VenueCreditMirrorRow)) == 0
    async with observation_factory.begin() as session:
        assert await session.scalar(select(func.count()).select_from(LedgerObservationRow)) == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LedgerObservationRow)
                .where(LedgerObservationRow.accepted.is_(False))
            )
            == 2
        )


@pytest.mark.asyncio
async def test_committed_command_fences_observation(seeded_fixture) -> None:  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        handle = await _begin(factory)
        async with factory.begin() as session:
            await record_attempt(
                session,
                SCOPE,
                Attempt(
                    uuid4(),
                    _D2,
                    "fUST",
                    "cell",
                    {},
                    UUID(_B),
                    UUID(_P),
                    {},
                    2,
                ),
            )
        first = _observation()
        async with factory.begin() as session:
            before = await session.scalar(select(func.count()).select_from(VenueOfferMirrorRow))
            result = await OBSERVATIONS.accept(
                session,
                SCOPE,
                handle,
                first,
                replace(first, finished_at_ms=4),
                3,
            )
            assert result.decision == "fenced"
            assert (
                await session.scalar(select(func.count()).select_from(VenueOfferMirrorRow))
                == before
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_absence_and_same_identity_terminal(observation_factory) -> None:
    await _accept(observation_factory, _observation(offers=(_offer(),), credits=(_credit(),)))
    await _accept(observation_factory, _observation())
    async with observation_factory.begin() as session:
        offer = await session.scalar(select(VenueOfferMirrorRow))
        credit = await session.scalar(select(VenueCreditMirrorRow))
        assert offer is not None and not offer.present_in_latest_accepted_snapshot
        assert credit is not None and not credit.present_in_latest_accepted_snapshot
        assert offer.terminal_evidence_id is None and credit.terminal_evidence_id is None
        assert offer.amount_remaining == Decimal("1") and credit.amount == Decimal("1")
    # Reappear, then vanish with wrong identities first. Neither can terminalize.
    await _accept(observation_factory, _observation(offers=(_offer(),), credits=(_credit(),)))
    await _accept(
        observation_factory,
        _observation(
            offer_history=(OfferHistory(_offer("wrong"), "canceled", 5),),
            credit_history=(CreditHistory(_credit("loan"), "closed", 5),),
        ),
    )
    async with observation_factory.begin() as session:
        offer = await session.scalar(select(VenueOfferMirrorRow))
        credit = await session.scalar(select(VenueCreditMirrorRow))
        assert offer is not None and offer.terminal_evidence_id is None
        assert credit is not None and credit.terminal_evidence_id is None
    terminal = _observation(
        offer_history=(OfferHistory(_offer(), "canceled", 6),),
        credit_history=(CreditHistory(_credit(), "closed", 6),),
    )
    await _accept(observation_factory, terminal)
    async with observation_factory.begin() as session:
        assert (
            await session.scalar(select(func.count()).select_from(LedgerObservationOfferHistoryRow))
            == 2
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(LedgerObservationCreditHistoryRow)
            )
            == 2
        )
        offer = await session.scalar(select(VenueOfferMirrorRow))
        credit = await session.scalar(select(VenueCreditMirrorRow))
        assert offer is not None and offer.terminal_kind == "canceled"
        assert credit is not None and credit.terminal_kind == "closed"


@pytest.mark.asyncio
async def test_terminal_reappearance(observation_factory) -> None:
    await _accept(
        observation_factory,
        _observation(offers=(_offer(),), credits=(_credit(), _credit("loan", "l1"))),
    )
    await _accept(
        observation_factory,
        _observation(
            offer_history=(OfferHistory(_offer(), "canceled", 5),),
            credit_history=(
                CreditHistory(_credit(), "closed", 5),
                CreditHistory(_credit("loan", "l1"), "closed", 5),
            ),
        ),
    )
    result = await _accept(
        observation_factory,
        _observation(offers=(_offer(),), credits=(_credit(), _credit("loan", "l1"))),
    )
    assert result.decision == "accepted"
    async with observation_factory.begin() as session:
        assert await session.scalar(select(func.count()).select_from(QuarantineMemberRow)) == 3
        # Same symbol: the three identities join one unresolved quarantine.
        assert await session.scalar(select(func.count()).select_from(QuarantineOpeningRow)) == 1
        for row in (await session.scalars(select(VenueOfferMirrorRow))).all():
            assert not row.present_in_latest_accepted_snapshot
        for row in (await session.scalars(select(VenueCreditMirrorRow))).all():
            assert not row.present_in_latest_accepted_snapshot
    # Still reappearing on the next acceptance: no new quarantine or member.
    await _accept(
        observation_factory,
        _observation(offers=(_offer(),), credits=(_credit(), _credit("loan", "l1"))),
    )
    async with observation_factory.begin() as session:
        assert await session.scalar(select(func.count()).select_from(QuarantineMemberRow)) == 3
        assert await session.scalar(select(func.count()).select_from(QuarantineOpeningRow)) == 1


@pytest.mark.asyncio
async def test_mirror_load_is_bounded(observation_factory) -> None:
    historic = tuple(_offer(f"old-{i}") for i in range(40))
    await _accept(observation_factory, _observation(offers=historic))
    await _accept(
        observation_factory,
        _observation(offer_history=tuple(OfferHistory(offer, "canceled", 5) for offer in historic)),
    )
    async with observation_factory.begin() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(VenueOfferMirrorRow)
                .where(VenueOfferMirrorRow.terminal_evidence_id.is_not(None))
            )
            == 40
        )
    loads = 0

    def loaded(_target, _context) -> None:
        nonlocal loads
        loads += 1

    event.listen(VenueOfferMirrorRow, "load", loaded)
    try:
        await _accept(observation_factory, _observation(offers=(_offer("new"),)))
    finally:
        event.remove(VenueOfferMirrorRow, "load", loaded)
    assert loads <= 1


@pytest.mark.asyncio
async def test_trade_coverage_is_required_for_acceptance(observation_factory) -> None:
    first = _observation(coverage=replace(_coverage(), trades_complete=False))
    result = await _accept(observation_factory, first, replace(first, finished_at_ms=4))
    assert result.decision == "incomplete_or_unequal"
    async with observation_factory.begin() as session:
        assert await session.scalar(select(func.count()).select_from(LedgerObservationRow)) == 0


@pytest.mark.asyncio
async def test_unknown_status_is_rejected_at_the_boundary(observation_factory) -> None:
    for bad in (
        _observation(offers=(replace(_offer(), status="ACTIVE"),)),  # type: ignore[arg-type]
        _observation(credits=(replace(_credit(), status="ACTIVE"),)),  # type: ignore[arg-type]
        _observation(credits=(replace(_credit(), mts_opening=None),)),  # type: ignore[arg-type]
        _observation(offer_history=(OfferHistory(_offer(), "CLOSED", 5),)),  # type: ignore[arg-type]
    ):
        with pytest.raises(ValueError, match="invalid"):
            await _accept(observation_factory, bad)


@pytest.mark.asyncio
async def test_funding_wallet_without_symbol_is_rejected(observation_factory) -> None:
    bad = replace(
        _observation(),
        wallets=(Wallet("funding", "UST", Decimal("3"), Decimal("9"), None),),
    )
    with pytest.raises(ValueError, match="funding wallet without symbol"):
        await _accept(observation_factory, bad)
