"""Halt 2 replay/conversion operator contracts.

The replay check is deliberately event-only: a current projection is only a
diagnostic comparison target, never an input to the rebuilt result.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.projector import derive_v2_event_id
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, VenueOfferStateRow
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from scripts.verify_projection_replay import (
    ReplayVerificationError,
    convert_pending_to_unknown,
    quarantine_orphans_after_replay,
    replay_event_log,
)
from tests.integration.test_orphan_quarantine_pg import _ACCOUNT as _ORPHAN_ACCOUNT
from tests.integration.test_orphan_quarantine_pg import _offer as _orphan_offer
from tests.integration.test_unknown_submit_pg import _ACCOUNT as _PENDING_ACCOUNT
from tests.integration.test_unknown_submit_pg import _seed_attempt

pytestmark = pytest.mark.integration

_ACCOUNT = UUID("00000000-0000-0000-0000-000000000043")
_ENV = "ci"


def _row(*, seq: int, payload: dict[str, object] | None = None) -> EventLogRow:
    body = payload or {
        "__schema_version__": 3,
        "__event_type__": "CREDIT_CLOSED",
        "account_id": str(_ACCOUNT),
        "symbol": "fUST",
        "amount": "1",
        "occurred_at_ms": 1_000 + seq,
        "event_id": "bf0e94ae-3a03-4810-ae8f-b3c931531ce0",
    }
    return EventLogRow(
        event_seq=seq,
        account_id=str(_ACCOUNT),
        exchange_account_id=_ACCOUNT,
        deployment_environment=_ENV,
        event_type="CREDIT_CLOSED",
        payload=body,
        occurred_at_ms=1_000 + seq,
        event_id=UUID("bf0e94ae-3a03-4810-ae8f-b3c931531ce0"),
        schema_version=int(body.get("__schema_version__", 2)),
    )


def test_empty_projection_replay_has_stable_hash_and_ignores_old_projection() -> None:
    """Changing a runtime projection must not change event-only replay evidence."""
    rows = [_row(seq=1), _row(seq=2)]

    first = replay_event_log(rows, account_id=_ACCOUNT, environment=_ENV)
    second = replay_event_log(rows, account_id=_ACCOUNT, environment=_ENV)

    assert first.row_counts == {"event_log": 2}
    assert first.content_hashes == second.content_hashes
    assert first.event_head == 2


def test_replay_derives_historical_v2_uuid_and_accepts_global_sequence_gaps() -> None:
    """Treating global rows from other accounts as a corruption is unsafe."""
    payload = {"amount": "1", "account_id": str(_ACCOUNT), "is_simulated": True}
    legacy = EventLogRow(
        event_seq=1,
        account_id=str(_ACCOUNT),
        exchange_account_id=_ACCOUNT,
        deployment_environment=_ENV,
        event_type="CREDIT_CLOSED",
        payload=payload,
        occurred_at_ms=1_001,
        schema_version=2,
    )

    result = replay_event_log([legacy], account_id=_ACCOUNT, environment=_ENV)

    assert result.event_ids == (derive_v2_event_id(
        event_seq=1,
        account_id=str(_ACCOUNT),
        deployment_environment=_ENV,
        event_type="CREDIT_CLOSED",
        occurred_at_ms=1_001,
        payload=payload,
    ),)
    scoped = replay_event_log(
        [_row(seq=1), _row(seq=3)], account_id=_ACCOUNT, environment=_ENV,
    )
    assert scoped.event_head == 3
    with pytest.raises(ReplayVerificationError, match="sequence ordering"):
        replay_event_log([_row(seq=3), _row(seq=1)], account_id=_ACCOUNT, environment=_ENV)


def test_replay_rejects_invalid_identity_and_hash_mismatch() -> None:
    """A malformed event identity or supplied event hash can never be advisory."""
    row = _row(seq=1)
    malformed = _row(seq=1)
    malformed.event_id = UUID("00000000-0000-0000-0000-000000000000")

    with pytest.raises(ReplayVerificationError, match="event identity"):
        replay_event_log([malformed], account_id=_ACCOUNT, environment=_ENV)
    with pytest.raises(ReplayVerificationError, match="event hash mismatch"):
        replay_event_log([row], account_id=_ACCOUNT, environment=_ENV, expected_event_hash="bad")


@pytest.mark.asyncio
async def test_pending_conversion_appends_one_unknown_and_is_idempotent(pg_session_factory) -> None:
    """Replacing writer serialization with executor work would reopen submit risk."""
    await _seed_attempt(pg_session_factory, unknown=False)

    first = await convert_pending_to_unknown(
        pg_session_factory, account_id=_PENDING_ACCOUNT, environment=_ENV, now_ms=5_000,
    )
    second = await convert_pending_to_unknown(
        pg_session_factory, account_id=_PENDING_ACCOUNT, environment=_ENV, now_ms=6_000,
    )

    async with pg_session_factory() as session:
        event_types = list(await session.scalars(select(EventLogRow.event_type).where(
            EventLogRow.exchange_account_id == _PENDING_ACCOUNT,
        ).order_by(EventLogRow.event_seq)))
        uncertainty = await session.scalar(select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.exchange_account_id == _PENDING_ACCOUNT,
            ExecutionUncertaintyRow.state == "open",
        ))
    assert first == 1
    assert second == 0
    assert event_types.count("SUBMIT_OUTCOME_UNKNOWN") == 1
    assert uncertainty is not None and uncertainty.kind == "submit_outcome_unknown"


@pytest.mark.asyncio
async def test_replayed_orphan_is_quarantined_without_losing_venue_object(pg_session_factory) -> None:
    """Skipping quarantine after a clean replay would silently un-block an orphan."""
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ORPHAN_ACCOUNT, venue="bitfinex", label="replay-orphan"))
        await session.commit()
    orphan = _orphan_offer("replay-orphan", "fXYZ", "7")
    snapshot = VenueSnapshotObserved(
        account_id=str(_ORPHAN_ACCOUNT), environment=_ENV,
        query_started_at_ms=1_000, query_finished_at_ms=1_001,
        offers=(
            VenueOfferObservation(
                venue_offer_id=orphan.venue_offer_id, symbol=orphan.symbol,
                amount_original=orphan.amount_original, amount_remaining=orphan.amount,
                rate=orphan.rate_decimal, period_days=orphan.period_days, status=orphan.status,
                mts_created=orphan.mts_created, mts_updated=orphan.mts_updated,
                offer_type=orphan.offer_type, flags={},
            ),
        ),
        credits=(), wallet_available={"fXYZ": orphan.amount},
        coverage=SnapshotCoverage(True, True, True),
    )
    async with pg_session_factory() as session:
        await PostgresEventStore(deployment_environment=_ENV).append_snapshot(session, snapshot)
        await session.commit()

    first = await quarantine_orphans_after_replay(
        pg_session_factory, account_id=_ORPHAN_ACCOUNT, environment=_ENV, now_ms=2_000,
    )
    second = await quarantine_orphans_after_replay(
        pg_session_factory, account_id=_ORPHAN_ACCOUNT, environment=_ENV, now_ms=3_000,
    )

    async with pg_session_factory() as session:
        venue = await session.get(
            VenueOfferStateRow,
            (_ORPHAN_ACCOUNT, _ENV, "replay-orphan"),
        )
        uncertainty = await session.scalar(select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.exchange_account_id == _ORPHAN_ACCOUNT,
            ExecutionUncertaintyRow.state == "open",
        ))
    assert first == 1
    assert second == 0
    assert venue is not None and venue.amount_remaining == orphan.amount
    assert uncertainty is not None and uncertainty.kind == "unattributed_venue_offer"
