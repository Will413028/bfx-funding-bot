import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
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
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

_SCID = UUID("11111111-1111-1111-1111-111111111111")
_CANONICAL_ACCOUNT = "550e8400-e29b-41d4-a716-446655440000"


def _ref(cid: int, voi: str | None = None) -> ReservationRef:
    return ReservationRef(
        execution_decision_id=f"d-store-{cid}", cid=cid,
        signal_correlation_id=_SCID, venue_offer_id=voi,
    )


async def _create_all(session: AsyncSession) -> None:
    bind = session.bind
    assert bind is not None
    async with bind.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)


def _claimed(seq: int) -> ReservationClaimed:
    cid = 100 + seq
    voi = f"v{seq}"
    return ReservationClaimed(cid=cid, venue_offer_id=voi, size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=seq, occurred_at_ms=1000 + seq, symbol="fUSD",
        reservation_ref=_ref(cid, voi))


async def test_append_inserts_event_row(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    event = _claimed(1)
    await store.append(sqlite_session, event)
    await sqlite_session.flush()
    rows = (await sqlite_session.execute(select(EventLogRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "RESERVATION_CLAIMED"
    assert rows[0].cid == 101
    assert rows[0].deployment_environment == "ci"
    assert rows[0].payload["size_usdt"] == "5"
    assert rows[0].event_id == event.event_id
    assert rows[0].schema_version == 3


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
        account_id="acct", is_simulated=True, venue_seq=6, occurred_at_ms=2000, symbol="fUSD",
        reservation_ref=_ref(105, "v5")))
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
        occurred_at_ms=1000, reservation_ref=_ref(820, "v820")))
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 820))).scalar_one()
    assert row.symbol == "fUSD"      # the offer's real currency, not a default


async def test_append_fill_dedup_skips_duplicate(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    fill = OrderFilled(cid=200, venue_offer_id="v9", credit_id=None, size_usdt=Decimal("2"),
        fill_rate=0.0, signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=9, occurred_at_ms=2000, symbol="fUSD", reservation_ref=_ref(200, "v9"))
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


async def test_exact_duplicate_claim_reference_is_projection_idempotent(
    sqlite_session: AsyncSession,
) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    reference = ReservationRef(
        execution_decision_id="d-projection", cid=991,
        signal_correlation_id=_SCID, venue_offer_id="v991",
    )
    claim = ReservationClaimed(
        cid=991, venue_offer_id="v991", size_usdt=Decimal("8"), symbol="fUST",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=1, occurred_at_ms=1000, reservation_ref=reference,
    )
    await store.append(sqlite_session, claim)
    await store.append(sqlite_session, ReservationClaimed(
        cid=991, venue_offer_id="v991", size_usdt=Decimal("8"), symbol="fUST",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=2, occurred_at_ms=1001, reservation_ref=reference,
    ))
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 991))).scalar_one()
    assert row.execution_decision_id == "d-projection"
    assert row.venue_offer_id == "v991"


async def test_conflicting_claim_reference_fails_without_mutating_projection(
    sqlite_session: AsyncSession,
) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    initial = ReservationRef(
        execution_decision_id="d-original", cid=992,
        signal_correlation_id=_SCID, venue_offer_id="v992",
    )
    await store.append(sqlite_session, ReservationClaimed(
        cid=992, venue_offer_id="v992", size_usdt=Decimal("8"), symbol="fUST",
        signal_correlation_id=_SCID, account_id=_CANONICAL_ACCOUNT, is_simulated=True,
        venue_seq=1, occurred_at_ms=1000, reservation_ref=initial,
    ))
    conflicting = ReservationRef(
        execution_decision_id="d-conflict", cid=992,
        signal_correlation_id=_SCID, venue_offer_id="v992",
    )
    with pytest.raises(RuntimeError, match="claim identity conflict"):
        await store.append(sqlite_session, ReservationClaimed(
            cid=992, venue_offer_id="v992", size_usdt=Decimal("8"), symbol="fUST",
            signal_correlation_id=_SCID, account_id=_CANONICAL_ACCOUNT, is_simulated=True,
            venue_seq=2, occurred_at_ms=1001, reservation_ref=conflicting,
        ))
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 992))).scalar_one()
    assert row.execution_decision_id == "d-original"


async def test_post_cutover_projection_reuses_historical_uuid_owned_claim(
    sqlite_session: AsyncSession,
) -> None:
    """New UUID-scoped writes must find rows whose audit realm is legacy text."""
    await _create_all(sqlite_session)
    sqlite_session.add(
        OfferClaimRow(
            cid=995,
            account_id="legacy-realm",
            exchange_account_id=UUID(_CANONICAL_ACCOUNT),
            deployment_environment="ci",
            state=RegistryState.CLAIMED.value,
            venue_offer_id="v995",
            symbol="fUST",
            size_usdt=Decimal("8"),
            signal_correlation_id=str(_SCID),
            execution_decision_id="d-995",
            occurred_at_ms=1000,
            last_updated_ms=1000,
            last_event_seq=1,
        )
    )
    await sqlite_session.flush()

    await PostgresEventStore(deployment_environment="ci").append(
        sqlite_session,
        ReservationReleased(
            cid=995,
            venue_offer_id="v995",
            size_usdt=Decimal("8"),
            reason="venue_cancel",
            signal_correlation_id=_SCID,
            account_id=_CANONICAL_ACCOUNT,
            is_simulated=True,
            venue_seq=2,
            occurred_at_ms=1100,
            symbol="fUST",
            reservation_ref=ReservationRef(
                execution_decision_id="d-995",
                cid=995,
                signal_correlation_id=_SCID,
                venue_offer_id="v995",
            ),
        ),
    )
    await sqlite_session.flush()

    rows = (
        await sqlite_session.execute(
            select(OfferClaimRow).where(
                OfferClaimRow.exchange_account_id == UUID(_CANONICAL_ACCOUNT),
                OfferClaimRow.cid == 995,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].account_id == "legacy-realm"
    assert rows[0].state == RegistryState.RELEASED.value


async def test_same_venue_offer_id_under_different_cid_fails_without_second_projection(
    sqlite_session: AsyncSession,
) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    first = ReservationRef(
        execution_decision_id="d-external-original", cid=993,
        signal_correlation_id=_SCID, venue_offer_id="v-shared",
    )
    await store.append(sqlite_session, ReservationClaimed(
        cid=993, venue_offer_id="v-shared", size_usdt=Decimal("8"), symbol="fUST",
                signal_correlation_id=_SCID, account_id=_CANONICAL_ACCOUNT, is_simulated=True,
        venue_seq=1, occurred_at_ms=1000, reservation_ref=first,
    ))
    conflicting = ReservationRef(
        execution_decision_id="d-external-conflict", cid=994,
        signal_correlation_id=_SCID, venue_offer_id="v-shared",
    )
    with pytest.raises(RuntimeError, match="claim identity conflict"):
        await store.append(sqlite_session, ReservationClaimed(
            cid=994, venue_offer_id="v-shared", size_usdt=Decimal("8"), symbol="fUST",
            signal_correlation_id=_SCID, account_id=_CANONICAL_ACCOUNT, is_simulated=True,
            venue_seq=2, occurred_at_ms=1001, reservation_ref=conflicting,
        ))
    rows = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.venue_offer_id == "v-shared"),
    )).scalars().all()
    assert [(row.cid, row.execution_decision_id) for row in rows] == [
        (993, "d-external-original"),
    ]


async def test_interleaved_same_cid_claims_are_idempotent_without_integrity_error(
    sqlite_engine: object,
) -> None:
    """Two writers that both observe no CID must converge atomically.

    The barrier deterministically exposes the old `get(); add()` TOCTOU window:
    both sessions return ``None`` before either can add the projection row.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as bootstrap:
        await _create_all(bootstrap)

    barrier = asyncio.Barrier(2)

    class _InterleavingSession:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session
            self._waited = False

        async def _interleave_once(self) -> None:
            if not self._waited:
                self._waited = True
                await barrier.wait()

        async def get(self, *args: object, **kwargs: object) -> object:
            result = await self._session.get(*args, **kwargs)  # type: ignore[arg-type]
            await self._interleave_once()
            return result

        async def execute(self, *args: object, **kwargs: object) -> object:
            result = await self._session.execute(*args, **kwargs)  # type: ignore[arg-type]
            await self._interleave_once()
            return result

        def __getattr__(self, name: str) -> object:
            return getattr(self._session, name)

    async with factory() as first, factory() as second:
        left = _InterleavingSession(first)
        right = _InterleavingSession(second)
        kwargs = {
            "cid": 995, "account_id": "acct", "state": RegistryState.CLAIMED,
            "venue_offer_id": "v-race", "symbol": "fUST", "size_usdt": Decimal("8"),
            "signal_correlation_id": str(_SCID), "execution_decision_id": "d-race",
            "occurred_at_ms": 1000, "last_updated_ms": 1000,
        }
        await asyncio.gather(
            PostgresEventStore(deployment_environment="ci")._upsert_claim(left, **kwargs),  # type: ignore[arg-type]
            PostgresEventStore(deployment_environment="ci")._upsert_claim(right, **kwargs),  # type: ignore[arg-type]
        )
        await first.flush()
        await second.flush()
        await first.commit()
        await second.commit()

    async with factory() as verify:
        rows = (await verify.execute(
            select(OfferClaimRow).where(OfferClaimRow.cid == 995),
        )).scalars().all()
    assert [(row.cid, row.execution_decision_id) for row in rows] == [(995, "d-race")]


async def test_position_state_tracks_event_time_deterministically(sqlite_session: AsyncSession) -> None:
    """last_updated_ms is sourced from the event's occurred_at_ms (domain time),
    not a wall-clock projection-time default — so it advances on every projected
    event and is reproducible via rebuild (event-sourcing determinism)."""
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationClaimed(
        cid=400, venue_offer_id="v400", size_usdt=Decimal("5"),
        signal_correlation_id=_SCID, account_id="acctT", is_simulated=True,
        venue_seq=1, occurred_at_ms=1000, symbol="fUSD", reservation_ref=_ref(400, "v400")))
    await sqlite_session.flush()
    ps = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acctT"))).scalar_one()
    assert ps.last_updated_ms == 1000

    # a later event advances last_updated_ms to that event's time
    await store.append(sqlite_session, ReservationReleased(
        cid=400, venue_offer_id="v400", size_usdt=Decimal("5"), reason="venue_cancel",
        signal_correlation_id=_SCID, account_id="acctT", is_simulated=True,
        venue_seq=2, occurred_at_ms=5000, symbol="fUSD", reservation_ref=_ref(400, "v400")))
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
        venue_seq=1, occurred_at_ms=9000, symbol="fUSD", reservation_ref=_ref(410, "v410")))
    # appended later (higher event_seq) but with an EARLIER event time
    await store.append(sqlite_session, ReservationReleased(
        cid=410, venue_offer_id="v410", size_usdt=Decimal("5"), reason="venue_cancel",
        signal_correlation_id=_SCID, account_id="acctOOO", is_simulated=True,
        venue_seq=2, occurred_at_ms=1000, symbol="fUSD", reservation_ref=_ref(410, "v410")))
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


async def test_rebuild_decodes_persisted_pre_task4_rows_as_explicit_legacy(
    sqlite_session: AsyncSession,
) -> None:
    """Only a persistent event_log row supplies historical replay provenance."""
    await _create_all(sqlite_session)
    intent_payload = {
        "cid": 880,
        "size_usdt": "5",
        "signal_correlation_id": str(_SCID),
        "account_id": "historical-acct",
        "is_simulated": False,
        "occurred_at_ms": 1000,
    }
    intent_row = EventLogRow(
        account_id="historical-acct",
        deployment_environment="prod",
        event_type="RESERVATION_INTENT",
        cid=880,
        venue_offer_id=None,
        venue_seq=None,
        payload=intent_payload,
        occurred_at_ms=1000,
    )
    claim_row = EventLogRow(
        account_id="historical-acct",
        deployment_environment="prod",
        event_type="RESERVATION_CLAIMED",
        cid=880,
        venue_offer_id="legacy-offer-880",
        venue_seq=1,
        payload={
            **intent_payload,
            "venue_offer_id": "legacy-offer-880",
            "amount": "5",
        },
        occurred_at_ms=1100,
    )
    sqlite_session.add_all([intent_row, claim_row])
    await sqlite_session.flush()

    from bfx_funding_bot.modules.execution.event_store.serialization import (
        deserialize_stored_event,
    )

    decoded = deserialize_stored_event(intent_row)
    assert decoded.is_legacy_uncorrelated is True  # type: ignore[attr-defined]
    assert decoded.execution_decision_id is None  # type: ignore[attr-defined]
    assert decoded.reservation_ref is None  # type: ignore[attr-defined]

    await PostgresEventStore(deployment_environment="prod").rebuild_snapshot_from_log(
        sqlite_session,
        account_id="historical-acct",
        deployment_environment="prod",
        symbol="fUST",
    )
    projection = (
        await sqlite_session.execute(
            select(OfferClaimRow).where(
                OfferClaimRow.account_id == "historical-acct",
                OfferClaimRow.deployment_environment == "prod",
                OfferClaimRow.cid == 880,
            )
        )
    ).scalar_one_or_none()
    assert projection is not None
    assert projection.state == "claimed"
    assert projection.venue_offer_id == "legacy-offer-880"
    assert projection.execution_decision_id is None


async def test_historical_replay_authorization_is_one_shot_and_payload_bound(
    sqlite_session: AsyncSession,
) -> None:
    """A legacy capability cannot construct a second or different event."""
    await _create_all(sqlite_session)
    payload = {
        "cid": 881,
        "size_usdt": "5",
        "signal_correlation_id": str(_SCID),
        "account_id": "historical-acct",
        "is_simulated": False,
        "occurred_at_ms": 1000,
    }
    row = EventLogRow(
        account_id="historical-acct",
        deployment_environment="prod",
        event_type="RESERVATION_INTENT",
        cid=881,
        venue_offer_id=None,
        venue_seq=None,
        payload=payload,
        occurred_at_ms=1000,
    )
    sqlite_session.add(row)
    await sqlite_session.flush()

    from bfx_funding_bot.modules.execution.event_store.replay import (
        HistoricalReplayProvenance,
    )
    from bfx_funding_bot.modules.execution.event_store.serialization import _decode_payload

    provenance = HistoricalReplayProvenance.from_stored_event(row)
    with pytest.raises(TypeError, match="does not match stored payload"):
        provenance.authorize_legacy_payload(
            event_type="RESERVATION_INTENT",
            payload={**payload, "cid": 999},
        )

    authorization = provenance.authorize_legacy_payload(
        event_type="RESERVATION_INTENT",
        payload=payload,
    )
    with pytest.raises(TypeError, match="already consumed"):
        provenance.authorize_legacy_payload(
            event_type="RESERVATION_INTENT",
            payload=payload,
        )

    decoded = _decode_payload(
        "RESERVATION_INTENT",
        payload,
        historical_authorization=authorization,
    )
    assert decoded.is_legacy_uncorrelated is True  # type: ignore[attr-defined]
    assert decoded.execution_decision_id is None  # type: ignore[attr-defined]
    assert decoded.reservation_ref is None  # type: ignore[attr-defined]

    with pytest.raises(TypeError, match="already consumed"):
        _decode_payload(
            "RESERVATION_INTENT",
            payload,
            historical_authorization=authorization,
        )


async def test_stored_event_decoder_rejects_transient_row() -> None:
    from bfx_funding_bot.modules.execution.event_store.serialization import (
        deserialize_stored_event,
    )

    row = EventLogRow(
        account_id="arbitrary",
        deployment_environment="ci",
        event_type="RESERVATION_INTENT",
        cid=1,
        venue_offer_id=None,
        venue_seq=None,
        payload={"cid": 1},
        occurred_at_ms=1,
    )

    with pytest.raises(TypeError, match="persistent event_log row"):
        deserialize_stored_event(row)


async def test_intent_then_failed_marks_failed_reserved_untouched(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=302, execution_decision_id="d-store-302", size_usdt=Decimal("8"), symbol="fUST", signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, occurred_at_ms=1000))
    await store.append(sqlite_session, ReservationFailed(
        cid=302, size_usdt=Decimal("8"), symbol="fUST", signal_correlation_id=_SCID,
        account_id="acctF", is_simulated=True, reason="submit_failed",
        occurred_at_ms=1100, reservation_ref=_ref(302)))
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
