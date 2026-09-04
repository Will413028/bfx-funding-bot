"""Pure checks for event-derived orphan quarantine candidates."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import (
    SnapshotCoverage,
    VenueSnapshotObserved,
)
from scripts.verify_projection_replay import _event_derived_orphan_candidates


def test_orphan_candidates_are_derived_from_latest_complete_snapshot_event() -> None:
    account_id = UUID("11111111-1111-1111-1111-111111111111")
    snapshot = VenueSnapshotObserved(
        account_id=str(account_id),
        environment="prod",
        query_started_at_ms=1_000,
        query_finished_at_ms=1_001,
        offers=(
            VenueOfferObservation(
                venue_offer_id="orphan-1",
                symbol="fUST",
                amount_original=Decimal("7"),
                amount_remaining=Decimal("7"),
                rate=Decimal("0.01"),
                period_days=2,
                status="active",
                mts_created=1_000,
                mts_updated=1_001,
            ),
        ),
        credits=(),
        wallet_available={"fUST": Decimal("7")},
        coverage=SnapshotCoverage(True, True, True),
    )
    # The event-only helper accepts stored rows, exactly as the operator path
    # reads them. No runtime VenueOfferStateRow/OfferClaimRow is supplied.
    row = EventLogRow(
        event_seq=10,
        account_id=str(account_id),
        exchange_account_id=account_id,
        deployment_environment="prod",
        event_type="VENUE_SNAPSHOT_OBSERVED",
        payload=serialize_event(snapshot),
        event_id=snapshot.event_id,
        schema_version=snapshot.schema_version,
        occurred_at_ms=snapshot.query_finished_at_ms,
    )

    candidates = _event_derived_orphan_candidates(
        [row], account_id=account_id, environment="prod"
    )

    assert [(item.venue_offer_id, item.symbol, item.amount) for item in candidates] == [
        ("orphan-1", "fUST", Decimal("7")),
    ]
