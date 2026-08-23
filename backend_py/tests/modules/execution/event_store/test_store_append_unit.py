from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationReleased,
)

_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def _create_all(session: AsyncSession) -> None:
    bind = session.bind
    assert bind is not None
    async with bind.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)


def _claimed(seq: int) -> ReservationClaimed:
    return ReservationClaimed(cid=100 + seq, venue_offer_id=f"v{seq}", size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=seq, occurred_at_ms=1000 + seq, symbol="fUSD")


async def test_append_inserts_event_row(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, _claimed(1))
    await sqlite_session.flush()
    rows = (await sqlite_session.execute(select(EventLogRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "RESERVATION_CLAIMED"
    assert rows[0].cid == 101
    assert rows[0].deployment_environment == "ci"
    assert rows[0].payload["size_usdt"] == "5"


async def test_claim_then_release_updates_offer_claims(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, _claimed(5))           # cid 105, v5
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 105))).scalar_one()
    assert row.state == "claimed"
    assert row.venue_offer_id == "v5"

    await store.append(sqlite_session, ReservationReleased(cid=105, venue_offer_id="v5",
        size_usdt=Decimal("5"), reason="venue_cancel", signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, venue_seq=6, occurred_at_ms=2000, symbol="fUSD"))
    await sqlite_session.flush()
    row2 = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 105))).scalar_one()
    assert row2.state == "released"


async def test_offer_claims_persists_symbol(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationClaimed(
        cid=820, venue_offer_id="v820", amount=Decimal("100"), symbol="fUSD",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000))
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 820))).scalar_one()
    assert row.symbol == "fUSD"      # the offer's real currency, not a default


async def test_append_fill_dedup_skips_duplicate(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    fill = OrderFilled(cid=200, venue_offer_id="v9", credit_id=None, size_usdt=Decimal("2"),
        fill_rate=0.0, signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=9, occurred_at_ms=2000, symbol="fUSD")
    inserted_first = await store.append(sqlite_session, fill)
    inserted_second = await store.append(sqlite_session, fill)  # same (voi, venue_seq)
    await sqlite_session.flush()
    count = (await sqlite_session.execute(
        select(func.count()).select_from(EventLogRow))).scalar_one()
    assert inserted_first is True
    assert inserted_second is False
    assert count == 1


async def test_intent_creates_pending_claim_with_null_voi(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=300, execution_decision_id="d-store-300", size_usdt=Decimal("8"), symbol="fUST", signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, occurred_at_ms=1000))
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 300))).scalar_one()
    assert row.state == "pending"
    assert row.venue_offer_id is None
    assert row.size_usdt == Decimal("8")
    assert row.execution_decision_id == "d-store-300"


async def test_intent_then_claimed_updates_same_cid_row(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=301, execution_decision_id="d-store-301", size_usdt=Decimal("8"), symbol="fUSD", signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, occurred_at_ms=1000))
    await store.append(sqlite_session, ReservationClaimed(
        cid=301, venue_offer_id="v301", size_usdt=Decimal("8"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=1, occurred_at_ms=1100, symbol="fUSD", reservation_ref=ReservationRef(
            execution_decision_id="d-store-301", cid=301,
            signal_correlation_id=_SCID, venue_offer_id="v301",
        )))
    await sqlite_session.flush()
    rows = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 301))).scalars().all()
    assert len(rows) == 1  # cid-keyed: PENDING row promoted in place, not a 2nd row
    assert rows[0].state == "claimed"
    assert rows[0].venue_offer_id == "v301"
    assert rows[0].execution_decision_id == "d-store-301"


async def test_position_state_tracks_event_time_deterministically(sqlite_session: AsyncSession) -> None:
    """last_updated_ms is sourced from the event's occurred_at_ms (domain time),
    not a wall-clock projection-time default — so it advances on every projected
    event and is reproducible via rebuild (event-sourcing determinism)."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationClaimed(
        cid=400, venue_offer_id="v400", size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acctT", is_simulated=True,
        venue_seq=1, occurred_at_ms=1000, symbol="fUSD"))
    await sqlite_session.flush()
    ps = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acctT"))).scalar_one()
    assert ps.last_updated_ms == 1000

    # a later event advances last_updated_ms to that event's time
    await store.append(sqlite_session, ReservationReleased(
        cid=400, venue_offer_id="v400", size_usdt=Decimal("5"), reason="venue_cancel",
        signal_correlation_id=_SCID, account_id="acctT", is_simulated=True,
        venue_seq=2, occurred_at_ms=5000, symbol="fUSD"))
    await sqlite_session.flush()
    ps2 = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acctT"))).scalar_one()
    assert ps2.last_updated_ms == 5000


async def test_last_updated_ms_follows_event_seq_not_max_time(sqlite_session: AsyncSession) -> None:
    """When event time is non-monotonic vs append order, last_updated_ms tracks
    the LAST-processed (highest event_seq) event's time, not max(occurred_at_ms).
    This is the semantic the migration backfill must mirror to stay reproducible."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationClaimed(
        cid=410, venue_offer_id="v410", size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acctOOO", is_simulated=True,
        venue_seq=1, occurred_at_ms=9000, symbol="fUSD"))
    # appended later (higher event_seq) but with an EARLIER event time
    await store.append(sqlite_session, ReservationReleased(
        cid=410, venue_offer_id="v410", size_usdt=Decimal("5"), reason="venue_cancel",
        signal_correlation_id=_SCID, account_id="acctOOO", is_simulated=True,
        venue_seq=2, occurred_at_ms=1000, symbol="fUSD"))
    await sqlite_session.flush()
    ps = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acctOOO"))).scalar_one()
    assert ps.last_updated_ms == 1000  # last-processed wins, NOT max(9000, 1000)


async def test_rebuild_reproduces_identical_position_state(sqlite_session: AsyncSession) -> None:
    """Rebuilding the snapshot from the log yields an identical last_updated_ms —
    proving the projection is a deterministic function of the event stream."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, _claimed(7))  # cid 107, occurred_at_ms=1007
    await sqlite_session.flush()
    before = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acct"))).scalar_one()
    assert before.last_updated_ms == 1007

    # ReservationClaimed.symbol defaults to "fUSD"; rebuild must match.
    await store.rebuild_snapshot_from_log(
        sqlite_session, account_id="acct", deployment_environment="ci", symbol="fUSD")
    await sqlite_session.flush()
    after = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acct",
        PositionStateRow.symbol == "fUSD"))).scalar_one()
    assert after.last_updated_ms == 1007  # identical — no wall-clock drift


async def test_intent_then_failed_marks_failed_reserved_untouched(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=302, execution_decision_id="d-store-302", size_usdt=Decimal("8"), symbol="fUST", signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, occurred_at_ms=1000))
    await store.append(sqlite_session, ReservationFailed(
        cid=302, size_usdt=Decimal("8"), symbol="fUST", signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, reason="submit_failed",
        occurred_at_ms=1100))
    await sqlite_session.flush()
    claim = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 302))).scalar_one()
    assert claim.state == "failed"
    ps = (await sqlite_session.execute(
        select(PositionStateRow).where(PositionStateRow.account_id == "acctF"))).scalar_one()
    assert ps.reserved == Decimal("0")   # FAILED never reserved capital
    assert ps.last_event_seq > 0              # high-water mark still advances


async def test_intent_projects_under_its_own_symbol(sqlite_session: AsyncSession) -> None:
    """Regression: Intent must route its projection by its OWN symbol (no fUSD
    leak from a dropped fallback). Intent carries no ledger effect, so reserved
    stays 0 while the position_state row is created under fUST only."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=701, execution_decision_id="d-store-701", size_usdt=Decimal("100"), symbol="fUST",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000))
    await sqlite_session.flush()
    ps = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acct"))).scalars().all()
    assert [p.symbol for p in ps] == ["fUST"]      # no fUSD row created
    assert ps[0].reserved == Decimal("0")          # intent has no ledger effect


async def test_credit_closed_is_audit_only(sqlite_session: AsyncSession) -> None:
    """CREDIT_CLOSED is attribution-only: event row written, but NO offer_claims
    row and NO position_state row/delta (reconcile stays the single writer of
    realized — ADR 2026-05-29)."""
    from bfx_funding_bot.modules.execution.events import CreditClosed
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, CreditClosed(
        symbol="fUST", credit_id=555, amount=Decimal("1338.03"), rate=0.000174,
        period_days=2, mts_create=1000, account_id="acctC", is_simulated=False,
        occurred_at_ms=2000))
    await sqlite_session.flush()
    rows = (await sqlite_session.execute(select(EventLogRow))).scalars().all()
    assert [r.event_type for r in rows] == ["CREDIT_CLOSED"]
    assert rows[0].payload["credit_id"] == 555
    claims = (await sqlite_session.execute(select(OfferClaimRow))).scalars().all()
    assert claims == []
    ps = (await sqlite_session.execute(select(PositionStateRow))).scalars().all()
    assert ps == []  # audit-only: never touches the ledger projection
