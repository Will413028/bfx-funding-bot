"""PostgreSQL integration tests for unattributed venue-offer quarantine."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import ProjectionWriteError
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    SnapshotCoverage,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.marketfeed.schemas import HealthStatus, HealthTarget

pytestmark = pytest.mark.integration

_ENV = "ci"
_ACCOUNT = UUID("00000000-0000-0000-0000-000000000042")


def _offer(venue_offer_id: str, symbol: str, amount: str, *, mts: int = 1_000):
    return ActiveFundingOffer(
        venue_offer_id=venue_offer_id,
        symbol=symbol,
        amount=Decimal(amount),
        amount_original=Decimal(amount),
        rate=0.0003,
        period_days=2,
        mts_created=mts,
        mts_updated=mts,
        status="ACTIVE",
        offer_type="LIMIT",
        flags=0,
    )


class _Auth:
    def __init__(self, offers) -> None:
        self.offers = list(offers)
        self.cancel_calls: list[str] = []

    async def get_active_funding_offers(self, *, ctx, symbol=None):
        del ctx, symbol
        return self.offers

    async def get_active_funding_loans(self, **kwargs):
        # Lent but not yet drawn into a position; none in this fixture.
        return []

    async def get_active_funding_credits(self, *, ctx, symbol=None):
        del ctx, symbol
        return []

    async def get_funding_available_all(self, *, ctx):
        del ctx
        return {"fUST": Decimal("100")}


class _Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class _Probe:
    def __init__(self) -> None:
        self.updates: list[tuple[HealthTarget, HealthStatus, dict[str, object]]] = []

    def record_heartbeat(self, _sub_task: str) -> None:
        return None

    def update(
        self,
        target: HealthTarget,
        status: HealthStatus,
        **fields: object,
    ) -> None:
        self.updates.append((target, status, fields))


async def _seed_account_and_known_claim(pg_session_factory) -> None:
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="orphan"))
        await session.commit()
    signal_id = uuid4()
    await EventStorePersister(
        store=PostgresEventStore(deployment_environment=_ENV),
        session_factory=pg_session_factory,
        compatibility_mode=True,
    ).persist(
        ReservationClaimed(
            symbol="fUST",
            cid=42,
            venue_offer_id="known",
            signal_correlation_id=signal_id,
            account_id=str(_ACCOUNT),
            is_simulated=False,
            amount=Decimal("40"),
            occurred_at_ms=900,
            reservation_ref=ReservationRef(
                execution_decision_id="known-decision",
                cid=42,
                signal_correlation_id=signal_id,
                venue_offer_id="known",
            ),
        )
    )


def _recovery(pg_session_factory, auth: _Auth, bus: _Bus | None = None) -> BootRecovery:
    return BootRecovery(
        store=PostgresEventStore(deployment_environment=_ENV),
        session_factory=pg_session_factory,
        auth_rest=auth,
        account_ctx=AccountContext(
            account_id=str(_ACCOUNT),
            credentials=Credentials(api_key="key", api_secret="secret"),
            allocation_cap_usdt=Decimal("1000"),
        ),
        deployment_environment=_ENV,
        bus=bus or _Bus(),
        symbols=["fUST", "fUSD"],
        clock=lambda: 5_000,
    )


@pytest.mark.asyncio
async def test_orphan_plus_known_offer_is_counted_blocked_and_never_given_synthetic_identity(
    pg_session_factory,
):
    """Dropping quarantine projection would understate unattributed exposure."""
    await _seed_account_and_known_claim(pg_session_factory)
    auth = _Auth([_offer("known", "fUST", "40"), _offer("orphan", "fXYZ", "7")])
    bus = _Bus()

    result = await _recovery(pg_session_factory, auth, bus).run()

    async with pg_session_factory() as session:
        claims = (await session.execute(select(OfferClaimRow))).scalars().all()
        uncertainty = (await session.execute(select(ExecutionUncertaintyRow))).scalar_one()
        orphan = await session.get(VenueOfferStateRow, (_ACCOUNT, _ENV, "orphan"))
        xyz_position = await session.get(PositionStateRow, (_ACCOUNT, _ENV, "fXYZ"))
        quarantine = (
            await session.execute(
                select(EventLogRow).where(
                    EventLogRow.event_type == "VENUE_OFFER_QUARANTINED"
                )
            )
        ).scalar_one()
    assert result.n_quarantined == 1
    assert len(claims) == 1 and claims[0].venue_offer_id == "known"
    assert uncertainty.kind == "unattributed_venue_offer"
    assert uncertainty.venue_offer_id == "orphan"
    assert uncertainty.state == "open"
    assert orphan is not None and orphan.cid is None
    assert orphan.execution_decision_id is None
    assert orphan.signal_correlation_id is None
    assert xyz_position is not None
    assert xyz_position.offered_amount == Decimal("7")
    assert xyz_position.uncertain_amount == Decimal("7")
    assert quarantine.cid is None
    assert "reservation_ref" not in quarantine.payload
    assert auth.cancel_calls == []
    assert any(getattr(event, "symbol", None) == "fUSD" for event in bus.events)


@pytest.mark.asyncio
async def test_quarantine_rebuild_preserves_fk_and_old_snapshot_cannot_reopen_terminal_offer(
    pg_session_factory,
):
    """Replay order and a delayed observation must preserve terminal monotonicity."""
    await _seed_account_and_known_claim(pg_session_factory)
    auth = _Auth([_offer("known", "fUST", "40"), _offer("orphan", "fXYZ", "7")])
    store = PostgresEventStore(deployment_environment=_ENV)
    await _recovery(pg_session_factory, auth).run()

    terminal = VenueSnapshotObserved(
        account_id=str(_ACCOUNT),
        environment=_ENV,
        query_started_at_ms=5_900,
        query_finished_at_ms=6_000,
        offers=(
            VenueOfferObservation(
                venue_offer_id="orphan",
                symbol="fXYZ",
                amount_original=Decimal("7"),
                amount_remaining=Decimal("0"),
                rate=Decimal("0.0003"),
                period_days=2,
                status="CANCELED",
                mts_created=1_000,
                mts_updated=6_000,
            ),
        ),
        credits=(),
        wallet_available={"fUST": Decimal("100")},
        coverage=SnapshotCoverage(True, True, True),
    )
    delayed_active = VenueSnapshotObserved(
        account_id=str(_ACCOUNT),
        environment=_ENV,
        query_started_at_ms=3_900,
        query_finished_at_ms=4_000,
        offers=(
            VenueOfferObservation(
                venue_offer_id="orphan",
                symbol="fXYZ",
                amount_original=Decimal("7"),
                amount_remaining=Decimal("7"),
                rate=Decimal("0.0003"),
                period_days=2,
                status="ACTIVE",
                mts_created=1_000,
                mts_updated=4_000,
            ),
        ),
        credits=(),
        wallet_available={"fUST": Decimal("100")},
        coverage=SnapshotCoverage(True, True, True),
    )
    async with pg_session_factory() as session:
        await store.append_snapshot(session, terminal)
        await store.append_snapshot(session, delayed_active)
        await session.commit()
    async with pg_session_factory() as session:
        await store.rebuild_snapshot_from_log(
            session,
            account_id=str(_ACCOUNT),
            deployment_environment=_ENV,
        )
        await session.commit()

    async with pg_session_factory() as session:
        orphan = await session.get(VenueOfferStateRow, (_ACCOUNT, _ENV, "orphan"))
        uncertainty = (await session.execute(select(ExecutionUncertaintyRow))).scalar_one()
    assert orphan is not None
    assert orphan.is_terminal is True
    assert orphan.status == "canceled"
    assert uncertainty.venue_offer_id == orphan.venue_offer_id
    assert uncertainty.opened_event_seq < orphan.last_seen_event_seq


@pytest.mark.asyncio
async def test_multiple_orphans_in_one_symbol_aggregate_into_one_bounded_uncertainty(
    pg_session_factory,
):
    """The one-open-scope DB invariant must not make a second orphan roll back."""
    await _seed_account_and_known_claim(pg_session_factory)
    auth = _Auth(
        [
            _offer("known", "fUST", "40"),
            _offer("orphan-a", "fXYZ", "7"),
            _offer("orphan-b", "fXYZ", "3"),
        ]
    )

    result = await _recovery(pg_session_factory, auth).run()

    async with pg_session_factory() as session:
        uncertainty = (await session.execute(select(ExecutionUncertaintyRow))).scalar_one()
        position = await session.get(PositionStateRow, (_ACCOUNT, _ENV, "fXYZ"))
    assert result.n_quarantined == 2
    assert uncertainty.intended_amount == Decimal("10")
    assert uncertainty.evidence["venue_offer_ids"] == ["orphan-a", "orphan-b"]
    assert position is not None
    assert position.offered_amount == Decimal("10")
    assert position.uncertain_amount == Decimal("10")


@pytest.mark.asyncio
async def test_unchanged_orphan_only_degrades_first_periodic_run(pg_session_factory):
    await _seed_account_and_known_claim(pg_session_factory)
    recovery = _recovery(
        pg_session_factory,
        _Auth([_offer("known", "fUST", "40"), _offer("orphan", "fXYZ", "7")]),
    )
    probe = _Probe()
    periodic = PeriodicReconcile(
        recovery=recovery,
        probe=probe,
        interval_s=90,
    )

    await periodic._tick()
    await periodic._tick()

    reconcile_statuses = [
        status
        for target, status, _fields in probe.updates
        if target == HealthTarget.RECONCILE
    ]
    async with pg_session_factory() as session:
        quarantine_events = (
            await session.execute(
                select(EventLogRow).where(
                    EventLogRow.event_type == "VENUE_OFFER_QUARANTINED"
                )
            )
        ).scalars().all()
    assert reconcile_statuses == [HealthStatus.DEGRADED, HealthStatus.HEALTHY]
    assert len(quarantine_events) == 1


@pytest.mark.asyncio
async def test_registered_account_standalone_quarantine_rolls_back(pg_session_factory):
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="standalone"))
        await session.commit()
    persister = EventStorePersister(
        store=PostgresEventStore(deployment_environment=_ENV),
        session_factory=pg_session_factory,
    )
    event = VenueOfferQuarantined(
        venue_offer_id="not-observed",
        symbol="fUST",
        amount=Decimal("5"),
        account_id=str(_ACCOUNT),
        observed_at_ms=2_000,
    )

    with pytest.raises(ProjectionWriteError, match="snapshot venue observation"):
        await persister.persist(event)

    async with pg_session_factory() as session:
        event_count = len((await session.execute(select(EventLogRow))).scalars().all())
        uncertainty_count = len(
            (await session.execute(select(ExecutionUncertaintyRow))).scalars().all()
        )
    assert event_count == 0
    assert uncertainty_count == 0
