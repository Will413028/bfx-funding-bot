"""Event-only Halt 2 projection replay and explicit uncertainty conversion.

``replay`` reads one account/environment event stream, validates its immutable
identity and sequence boundary, then emits deterministic content hashes.  It
does not read or mutate runtime projections; optional runtime row counts are
reported only as a diagnostic comparison after event-only replay succeeds.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.execution.boot_recovery import (
    convert_pending_to_unknown as _convert_pending_to_unknown,
)
from bfx_funding_bot.modules.execution.event_store.projector import projection_content_hash
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_stored_event,
    stored_event_identity,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import (
    DEFAULT_PROJECTOR_VERSION,
    AccountEventWriter,
)
from bfx_funding_bot.modules.execution.events import VenueOfferQuarantined
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow

EXIT_SUCCESS = 0
EXIT_VERIFICATION_FAILED = 3


class ReplayVerificationError(ValueError):
    """The event stream cannot safely produce an authoritative replay."""


@dataclass(frozen=True, slots=True)
class ReplayReport:
    account_id: str
    environment: str
    projector_version: str
    event_head: int | None
    event_hash: str
    row_counts: dict[str, int]
    content_hashes: dict[str, str]
    event_ids: tuple[UUID, ...]
    diagnostic_row_counts: dict[str, int] | None = None


def replay_event_log(
    rows: Sequence[EventLogRow],
    *,
    account_id: UUID,
    environment: str,
    projector_version: str = DEFAULT_PROJECTOR_VERSION,
    expected_event_hash: str | None = None,
) -> ReplayReport:
    """Build an isolated, deterministic event projection from durable rows.

    The ephemeral projection deliberately contains only values derivable from
    event_log: verified identity, row count and canonical content hash.  It is
    sufficient for the Halt 2 operator gate and never uses wall-clock time,
    environment variables, live venue calls or a prior projection as truth.
    """
    if not projector_version.strip():
        raise ReplayVerificationError("missing projector version")
    previous_seq: int | None = None
    event_ids: list[UUID] = []
    canonical_rows: list[dict[str, object]] = []
    for row in rows:
        if row.exchange_account_id != account_id or row.deployment_environment != environment:
            raise ReplayVerificationError("event stream scope mismatch")
        if row.event_seq is None:
            raise ReplayVerificationError("event stream has missing event sequence")
        # ``event_seq`` is global, not account-local.  A scoped replay may
        # legitimately skip rows written by another account/environment; only
        # duplicate or decreasing rows prove that this supplied stream is not
        # a valid ordered subset of the global log.
        if previous_seq is not None and row.event_seq <= previous_seq:
            raise ReplayVerificationError(
                f"non-contiguous event sequence ordering: previous={previous_seq}, got={row.event_seq}"
            )
        previous_seq = row.event_seq
        try:
            identity = stored_event_identity(row)
        except (TypeError, ValueError) as exc:
            raise ReplayVerificationError(f"invalid event identity at seq={row.event_seq}: {exc}") from exc
        event_ids.append(identity.event_id)
        canonical_rows.append({
            "event_seq": row.event_seq,
            "event_id": str(identity.event_id),
            "event_type": row.event_type,
            "occurred_at_ms": row.occurred_at_ms,
            "payload": row.payload,
        })
    event_hash = projection_content_hash(canonical_rows)
    if expected_event_hash is not None and event_hash != expected_event_hash:
        raise ReplayVerificationError("event hash mismatch")
    return ReplayReport(
        account_id=str(account_id),
        environment=environment,
        projector_version=projector_version,
        event_head=previous_seq,
        event_hash=event_hash,
        row_counts={"event_log": len(canonical_rows)},
        content_hashes={"event_log": event_hash},
        event_ids=tuple(event_ids),
    )


async def _diagnostic_projection_counts(
    session: AsyncSession, *, account_id: UUID, environment: str
) -> dict[str, int]:
    statements = {
        "offer_claims": select(func.count()).select_from(OfferClaimRow).where(
            OfferClaimRow.exchange_account_id == account_id,
            OfferClaimRow.deployment_environment == environment,
        ),
        "position_state": select(func.count()).select_from(PositionStateRow).where(
            PositionStateRow.exchange_account_id == account_id,
            PositionStateRow.deployment_environment == environment,
        ),
        "venue_offer_state": select(func.count()).select_from(VenueOfferStateRow).where(
            VenueOfferStateRow.exchange_account_id == account_id,
            VenueOfferStateRow.deployment_environment == environment,
        ),
        "venue_credit_state": select(func.count()).select_from(VenueCreditStateRow).where(
            VenueCreditStateRow.exchange_account_id == account_id,
            VenueCreditStateRow.deployment_environment == environment,
        ),
        "execution_uncertainties": select(func.count())
        .select_from(ExecutionUncertaintyRow)
        .where(
            ExecutionUncertaintyRow.exchange_account_id == account_id,
            ExecutionUncertaintyRow.deployment_environment == environment,
        ),
    }
    return {
        name: int(await session.scalar(statement) or 0)
        for name, statement in statements.items()
    }


async def replay_one_account(
    session: AsyncSession, *, account_id: UUID, environment: str,
    projector_version: str, expected_event_hash: str | None = None,
) -> ReplayReport:
    """Read event_log once and attach runtime counts strictly as diagnostics."""
    rows = list(await session.scalars(select(EventLogRow).where(
        EventLogRow.exchange_account_id == account_id,
        EventLogRow.deployment_environment == environment,
    ).order_by(EventLogRow.event_seq.asc())))
    for row in rows:
        try:
            # Stored rows are the only trusted boundary permitted to invoke a
            # historical upcaster.  Fail before producing operator evidence.
            deserialize_stored_event(row)
        except (TypeError, ValueError) as exc:
            raise ReplayVerificationError(
                f"missing or invalid historical upcaster at seq={row.event_seq}: {exc}"
            ) from exc
    report = replay_event_log(
        rows, account_id=account_id, environment=environment,
        projector_version=projector_version, expected_event_hash=expected_event_hash,
    )
    return ReplayReport(**{**asdict(report), "diagnostic_row_counts": await _diagnostic_projection_counts(
        session, account_id=account_id, environment=environment,
    )})


async def convert_pending_to_unknown(
    session_factory: async_sessionmaker[AsyncSession], *, account_id: UUID,
    environment: str, now_ms: int,
) -> int:
    """Explicit write path: append UNKNOWN outcomes through AccountEventWriter."""
    async with session_factory() as session, session.begin():
        return await _convert_pending_to_unknown(
            session, account_id=str(account_id), environment=environment, now_ms=now_ms,
        )


async def quarantine_orphans_after_replay(
    session_factory: async_sessionmaker[AsyncSession], *, account_id: UUID,
    environment: str, now_ms: int,
) -> int:
    """Append quarantine breadcrumbs for replayed, unattributed active offers.

    The normalized venue-object rows came from the replayed snapshot and are
    deliberately not altered here.  An open uncertainty continues to block its
    exact account/symbol scope; this operator command never resumes or changes
    the persistent halt state.
    """
    async with session_factory() as session, session.begin():
        offers = list(await session.scalars(select(VenueOfferStateRow).where(
            VenueOfferStateRow.exchange_account_id == account_id,
            VenueOfferStateRow.deployment_environment == environment,
            VenueOfferStateRow.is_terminal.is_(False),
        ).order_by(VenueOfferStateRow.venue_offer_id.asc())))
        claimed = set(await session.scalars(select(OfferClaimRow.venue_offer_id).where(
            OfferClaimRow.exchange_account_id == account_id,
            OfferClaimRow.deployment_environment == environment,
            OfferClaimRow.state == "claimed",
            OfferClaimRow.venue_offer_id.is_not(None),
        )))
        events: list[object] = [
            VenueOfferQuarantined(
                venue_offer_id=offer.venue_offer_id,
                symbol=offer.symbol,
                amount=offer.amount_remaining,
                account_id=str(account_id),
                observed_at_ms=now_ms,
            )
            for offer in offers
            if offer.venue_offer_id not in claimed
        ]
        if not events:
            return 0
        results = await AccountEventWriter(
            store=PostgresEventStore(deployment_environment=environment)
        ).append_batch(session, events)
        return sum(result.persisted for result in results)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("replay", "convert-pending", "quarantine"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--projector-version", default=DEFAULT_PROJECTOR_VERSION)
    parser.add_argument("--expected-event-hash")
    parser.add_argument("--now-ms", type=int)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    account_id = UUID(args.account_id)
    engine = make_engine(Settings())
    factory = make_session_factory(engine)
    try:
        if args.command == "convert-pending":
            if args.now_ms is None:
                raise ValueError("convert-pending requires --now-ms")
            return {"converted_pending": await convert_pending_to_unknown(
                factory, account_id=account_id, environment=args.environment, now_ms=args.now_ms,
            )}
        if args.command == "quarantine":
            if args.now_ms is None:
                raise ValueError("quarantine requires --now-ms")
            return {"quarantined_orphans": await quarantine_orphans_after_replay(
                factory, account_id=account_id, environment=args.environment, now_ms=args.now_ms,
            )}
        async with factory() as session:
            return asdict(await replay_one_account(
                session, account_id=account_id, environment=args.environment,
                projector_version=args.projector_version,
                expected_event_hash=args.expected_event_hash,
            ))
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = asyncio.run(_run(_parser().parse_args(argv)))
    except (RuntimeError, ValueError, ReplayVerificationError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return EXIT_VERIFICATION_FAILED
    print(json.dumps(result, sort_keys=True, default=str))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
