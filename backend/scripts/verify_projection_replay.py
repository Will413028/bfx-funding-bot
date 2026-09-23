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
from decimal import Decimal
from typing import Any, TypeGuard
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.execution.boot_recovery import (
    convert_pending_to_unknown as _convert_pending_to_unknown,
)
from bfx_funding_bot.modules.execution.event_store.entities import is_terminal_offer_status
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _PROJECTION_REPORT_TABLE_NAMES as _PROJECTION_REPORT_TABLE_NAMES,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _PROJECTOR_IMPLEMENTATIONS as _PROJECTOR_IMPLEMENTATIONS,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _REPORT_TABLE_NAMES as _REPORT_TABLE_NAMES,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _SURROGATE_PROJECTION_COLUMNS as _SURROGATE_PROJECTION_COLUMNS,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _TEMPORARY_PROJECTION_MODELS as _TEMPORARY_PROJECTION_MODELS,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _TEMPORARY_TABLE_NAMES as _TEMPORARY_TABLE_NAMES,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _VOLATILE_PROJECTION_COLUMNS as _VOLATILE_PROJECTION_COLUMNS,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    ReplayReport as ReplayReport,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    ReplayVerificationError as ReplayVerificationError,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _archive_projection_rows as _archive_projection_rows,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _canonical_projection_row as _canonical_projection_row,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _canonical_value as _canonical_value,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _projection_evidence as _projection_evidence,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _projector_implementation as _projector_implementation,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    _replay_into_empty_temporary_projection as _replay_into_empty_temporary_projection,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    replay_captured_rows as replay_captured_rows,
)
from bfx_funding_bot.modules.execution.event_store.replay_verification import (
    replay_event_log as replay_event_log,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_stored_event,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import (
    DEFAULT_PROJECTOR_VERSION,
    AccountEventWriter,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    SubmitMatchedToVenueOffer,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.projection_cutover.diagnostics import (
    DifferenceSink,
    RowCollector,
)

EXIT_SUCCESS = 0
EXIT_VERIFICATION_FAILED = 3

@dataclass(frozen=True, slots=True)
class ReplayedOrphanCandidate:
    """An active venue offer derived solely from a complete snapshot event."""

    venue_offer_id: str
    symbol: str
    amount: Decimal


def _diagnostic_diff(
    *,
    old_row_counts: dict[str, int],
    old_content_hashes: dict[str, str],
    replayed_row_counts: dict[str, int],
    replayed_content_hashes: dict[str, str],
) -> dict[str, dict[str, int | str | bool]]:
    """Produce bounded hash/count evidence; never return runtime row contents."""
    return {
        name: {
            "old_count": old_row_counts[name],
            "replayed_count": replayed_row_counts[name],
            "old_hash": old_content_hashes[name],
            "replayed_hash": replayed_content_hashes[name],
            "matches": (
                old_row_counts[name] == replayed_row_counts[name]
                and old_content_hashes[name] == replayed_content_hashes[name]
            ),
        }
        for name, _ in _TEMPORARY_PROJECTION_MODELS
    }


async def _diagnostic_projection_evidence(
    session: AsyncSession, *, account_id: UUID, environment: str,
    streaming: bool = False,
) -> tuple[dict[str, int], dict[str, str]]:
    """Read the old runtime projection only after event replay succeeds."""
    row_counts, content_hashes, _, _ = await _projection_evidence(
        session,
        account_id=account_id,
        environment=environment,
        streaming=streaming,
    )
    return row_counts, content_hashes


async def replay_one_account(
    session: AsyncSession, *, account_id: UUID, environment: str,
    projector_version: str, expected_event_hash: str | None = None,
    difference_sink: DifferenceSink | None = None,
    diagnostic_collector: RowCollector | None = None,
) -> ReplayReport:
    """Read event_log once and attach runtime counts strictly as diagnostics."""
    # Bind events and original full rows to one source MVCC snapshot. Never
    # silently upgrade an already-started READ COMMITTED transaction.
    if (
        difference_sink is not None or diagnostic_collector is not None
    ) and not session.in_transaction():
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    rows = list(await session.scalars(select(EventLogRow).where(
        EventLogRow.exchange_account_id == account_id,
        EventLogRow.deployment_environment == environment,
    ).order_by(EventLogRow.event_seq.asc())))
    report = await replay_captured_rows(
        session, rows=rows, account_id=account_id, environment=environment,
        projector_version=projector_version, expected_event_hash=expected_event_hash,
        difference_sink=difference_sink,
        diagnostic_collector=diagnostic_collector,
    )
    old_row_counts, old_content_hashes = await _diagnostic_projection_evidence(
        session,
        account_id=account_id,
        environment=environment,
        streaming=difference_sink is not None,
    )
    return ReplayReport(
        **{
            **asdict(report),
            "diagnostic_old_row_counts": old_row_counts,
            "diagnostic_old_content_hashes": old_content_hashes,
            "diagnostic_diff": _diagnostic_diff(
                old_row_counts=old_row_counts,
                old_content_hashes=old_content_hashes,
                replayed_row_counts=report.row_counts,
                replayed_content_hashes=report.content_hashes,
            ),
        }
    )


def render_replay_report(report: ReplayReport) -> dict[str, object]:
    """Return the bounded operator report, excluding identities and raw rows.

    All emitted values are fixed-size identifiers, counts, hashes, and boolean
    diff evidence.  Event payloads, old projection content, Authorization
    headers, API secrets, and raw venue responses are never serialised here.
    """
    return {
        "account_id": report.account_id,
        "environment": report.environment,
        "projector_version": report.projector_version,
        "event_head": report.event_head,
        "event_hash": report.event_hash,
        "row_counts": _bounded_counts(report.row_counts, _REPORT_TABLE_NAMES),
        "content_hashes": _bounded_hashes(report.content_hashes, _REPORT_TABLE_NAMES),
        "diagnostic_old_row_counts": _bounded_counts(
            report.diagnostic_old_row_counts or {}, _PROJECTION_REPORT_TABLE_NAMES,
        ),
        "diagnostic_old_content_hashes": _bounded_hashes(
            report.diagnostic_old_content_hashes or {}, _PROJECTION_REPORT_TABLE_NAMES,
        ),
        "diagnostic_diff": _bounded_diffs(report.diagnostic_diff or {}),
    }


def _bounded_counts(values: dict[str, int], names: tuple[str, ...]) -> dict[str, int]:
    """Keep only known tables and non-negative integer counts in operator output."""
    return {
        name: value
        for name in names
        if isinstance(value := values.get(name), int) and value >= 0
    }


def _is_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_hashes(values: dict[str, str], names: tuple[str, ...]) -> dict[str, str]:
    """Keep fixed-width hashes only; never promote arbitrary diagnostic data."""
    bounded: dict[str, str] = {}
    for name in names:
        value = values.get(name)
        if _is_sha256(value):
            bounded[name] = value
    return bounded


def _bounded_diffs(
    values: dict[str, dict[str, int | str | bool]],
) -> dict[str, dict[str, int | str | bool]]:
    """Whitelist diff fields so malformed reports cannot smuggle raw responses."""
    bounded: dict[str, dict[str, int | str | bool]] = {}
    for name in _PROJECTION_REPORT_TABLE_NAMES:
        value = values.get(name)
        if value is None:
            continue
        old_count = value.get("old_count")
        replayed_count = value.get("replayed_count")
        old_hash = value.get("old_hash")
        replayed_hash = value.get("replayed_hash")
        matches = value.get("matches")
        if not (
            isinstance(old_count, int)
            and old_count >= 0
            and isinstance(replayed_count, int)
            and replayed_count >= 0
            and _is_sha256(old_hash)
            and _is_sha256(replayed_hash)
            and isinstance(matches, bool)
        ):
            continue
        bounded[name] = {
            "old_count": old_count,
            "replayed_count": replayed_count,
            "old_hash": old_hash,
            "replayed_hash": replayed_hash,
            "matches": matches,
        }
    return bounded


async def convert_pending_to_unknown(
    session_factory: async_sessionmaker[AsyncSession], *, account_id: UUID,
    environment: str, now_ms: int,
) -> int:
    """Explicit write path: append UNKNOWN outcomes through AccountEventWriter."""
    async with session_factory() as session, session.begin():
        return await _convert_pending_to_unknown(
            session, account_id=str(account_id), environment=environment, now_ms=now_ms,
        )


def _event_derived_orphan_candidates(
    rows: Sequence[EventLogRow], *, account_id: UUID, environment: str
) -> tuple[ReplayedOrphanCandidate, ...]:
    """Find unattributed active offers without consulting runtime projections."""
    decoded: list[tuple[int, object]] = []
    for row in rows:
        if (
            row.exchange_account_id != account_id
            or row.deployment_environment != environment
            or row.event_seq is None
        ):
            raise ReplayVerificationError("orphan candidate event scope is invalid")
        try:
            decoded.append((row.event_seq, deserialize_stored_event(row)))
        except (TypeError, ValueError) as exc:
            raise ReplayVerificationError(
                f"orphan candidate event is not replayable at seq={row.event_seq}"
            ) from exc

    snapshots = [
        (event_seq, event)
        for event_seq, event in decoded
        if isinstance(event, VenueSnapshotObserved)
    ]
    if not snapshots:
        raise ReplayVerificationError("orphan quarantine requires a venue snapshot")
    _snapshot_seq, snapshot = max(snapshots, key=lambda item: item[0])
    if not (
        snapshot.coverage.active_offers_complete
        and snapshot.coverage.active_credits_complete
        and snapshot.coverage.wallets_complete
    ):
        raise ReplayVerificationError(
            "orphan quarantine requires complete venue snapshot coverage"
        )

    attributed_offer_ids = {
        event.venue_offer_id
        for _event_seq, event in decoded
        if isinstance(event, (ReservationClaimed, SubmitMatchedToVenueOffer))
        and event.venue_offer_id
    }
    candidates = [
        ReplayedOrphanCandidate(
            venue_offer_id=offer.venue_offer_id,
            symbol=offer.symbol,
            amount=offer.amount_remaining,
        )
        for offer in snapshot.offers
        if not is_terminal_offer_status(offer.status)
        and offer.venue_offer_id not in attributed_offer_ids
        and offer.cid is None
        and offer.execution_decision_id is None
    ]
    return tuple(sorted(candidates, key=lambda item: item.venue_offer_id))


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
        writer = AccountEventWriter(
            store=PostgresEventStore(deployment_environment=environment)
        )
        # Hold the same account transaction lock as production event writers
        # while taking the replay fence and appending quarantine breadcrumbs.
        await writer.acquire_lock(session, account_id=account_id)
        rows = list(await session.scalars(select(EventLogRow).where(
            EventLogRow.exchange_account_id == account_id,
            EventLogRow.deployment_environment == environment,
        ).order_by(EventLogRow.event_seq.asc())))
        replay = replay_event_log(
            rows, account_id=account_id, environment=environment,
            projector_version=DEFAULT_PROJECTOR_VERSION,
        )
        # Exercise the same empty-projector implementation before any
        # quarantine event is appended.  Runtime rows are never candidate input.
        await _replay_into_empty_temporary_projection(
            session,
            rows=rows,
            account_id=account_id,
            environment=environment,
            projector_version=replay.projector_version,
        )
        candidates = _event_derived_orphan_candidates(
            rows, account_id=account_id, environment=environment
        )
        events: list[object] = [
            VenueOfferQuarantined(
                venue_offer_id=candidate.venue_offer_id,
                symbol=candidate.symbol,
                amount=candidate.amount,
                account_id=str(account_id),
                observed_at_ms=now_ms,
            )
            for candidate in candidates
        ]
        if not events:
            return 0
        results = await writer.append_batch(session, events)
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
            return render_replay_report(await replay_one_account(
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
