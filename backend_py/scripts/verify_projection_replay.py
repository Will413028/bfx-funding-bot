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
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, TypeGuard
from uuid import UUID

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.execution.boot_recovery import (
    convert_pending_to_unknown as _convert_pending_to_unknown,
)
from bfx_funding_bot.modules.execution.event_store.canonical import (
    canonical_event_hash,
    canonical_event_record,
)
from bfx_funding_bot.modules.execution.event_store.entities import is_terminal_offer_status
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
    ProjectionHeadRow,
    ReconcileObservationRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
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
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)

EXIT_SUCCESS = 0
EXIT_VERIFICATION_FAILED = 3

# This is deliberately a registry of implementations, rather than an
# ``if version`` guard.  A replay report is only meaningful when the operator
# selected a projector that this binary can actually run.
_PROJECTOR_IMPLEMENTATIONS: dict[str, type[PostgresEventStore]] = {
    DEFAULT_PROJECTOR_VERSION: PostgresEventStore,
}

# Keep temporary relation names static: they are SQL identifiers, never
# operator input.  PostgreSQL temporary tables shadow only these runtime
# projections on the isolated connection; the caller's session is untouched.
_TEMPORARY_PROJECTION_MODELS: tuple[tuple[str, type[object]], ...] = (
    ("offer_claims", OfferClaimRow),
    ("position_state", PositionStateRow),
    ("venue_offer_state", VenueOfferStateRow),
    ("venue_credit_state", VenueCreditStateRow),
    ("projection_heads", ProjectionHeadRow),
    ("reconcile_observation", ReconcileObservationRow),
    ("submission_attempts", SubmissionAttemptRow),
    ("execution_uncertainties", ExecutionUncertaintyRow),
)
_TEMPORARY_TABLE_NAMES = (
    "event_log",
    *(name for name, _ in _TEMPORARY_PROJECTION_MODELS),
)
_PROJECTION_REPORT_TABLE_NAMES = tuple(
    name for name, _ in _TEMPORARY_PROJECTION_MODELS
)
_REPORT_TABLE_NAMES = ("event_log", *_PROJECTION_REPORT_TABLE_NAMES)
_VOLATILE_PROJECTION_COLUMNS = frozenset({
    "recorded_at", "updated_at", "opened_at", "resolved_at",
})
_SURROGATE_PROJECTION_COLUMNS: dict[str, frozenset[str]] = {
    "reconcile_observation": frozenset({"id"}),
}


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
    diagnostic_old_row_counts: dict[str, int] | None = None
    diagnostic_old_content_hashes: dict[str, str] | None = None
    diagnostic_diff: dict[str, dict[str, int | str | bool]] | None = None
    # Internal event-derived exposure facts used by the bounded canary verifier.
    # They are intentionally omitted from ``render_replay_report``.
    replayed_offer_exposure_by_symbol: dict[str, Decimal] = field(default_factory=dict)
    replayed_credit_exposure_by_symbol: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReplayedOrphanCandidate:
    """An active venue offer derived solely from a complete snapshot event."""

    venue_offer_id: str
    symbol: str
    amount: Decimal


def _projector_implementation(projector_version: str) -> type[PostgresEventStore]:
    try:
        return _PROJECTOR_IMPLEMENTATIONS[projector_version]
    except KeyError:
        raise ReplayVerificationError(
            f"unsupported projector version: {projector_version!r}"
        ) from None


def _canonical_value(value: object) -> object:
    """Return a stable projection-hash value without serializing report data."""
    if isinstance(value, UUID):
        return str(value)
    return value


def _canonical_projection_row(row: object) -> dict[str, object]:
    table = row.__table__  # type: ignore[attr-defined]  # SQLAlchemy mapped row.
    excluded_columns = _VOLATILE_PROJECTION_COLUMNS | _SURROGATE_PROJECTION_COLUMNS.get(
        table.name, frozenset()
    )
    return {
        row.__mapper__.get_property_by_column(column).key: _canonical_value(  # type: ignore[attr-defined]
            getattr(row, row.__mapper__.get_property_by_column(column).key)  # type: ignore[attr-defined]
        )
        for column in table.columns
        if column.key not in excluded_columns
    }


async def _projection_evidence(
    session: AsyncSession, *, account_id: UUID, environment: str,
) -> tuple[dict[str, int], dict[str, str], dict[str, Decimal], dict[str, Decimal]]:
    """Hash scoped projection rows without retaining or reporting their contents."""
    row_counts: dict[str, int] = {}
    content_hashes: dict[str, str] = {}
    for name, model in _TEMPORARY_PROJECTION_MODELS:
        rows = list(await session.scalars(select(model).where(
            model.exchange_account_id == account_id,  # type: ignore[attr-defined]
            model.deployment_environment == environment,  # type: ignore[attr-defined]
        )))
        canonical_rows = sorted(
            (_canonical_projection_row(row) for row in rows),
            key=lambda value: projection_content_hash([value]),
        )
        row_counts[name] = len(canonical_rows)
        content_hashes[name] = projection_content_hash(canonical_rows)
    offer_exposure: dict[str, Decimal] = {}
    for offer_row in await session.scalars(select(VenueOfferStateRow).where(
        VenueOfferStateRow.exchange_account_id == account_id,
        VenueOfferStateRow.deployment_environment == environment,
        VenueOfferStateRow.is_terminal.is_(False),
    )):
        offer_exposure[offer_row.symbol] = (
            offer_exposure.get(offer_row.symbol, Decimal("0"))
            + Decimal(str(offer_row.amount_remaining))
        )
    credit_exposure: dict[str, Decimal] = {}
    for credit_row in await session.scalars(select(VenueCreditStateRow).where(
        VenueCreditStateRow.exchange_account_id == account_id,
        VenueCreditStateRow.deployment_environment == environment,
        VenueCreditStateRow.is_terminal.is_(False),
    )):
        credit_exposure[credit_row.symbol] = (
            credit_exposure.get(credit_row.symbol, Decimal("0"))
            + Decimal(str(credit_row.amount))
        )
    return row_counts, content_hashes, offer_exposure, credit_exposure


async def _replay_into_empty_temporary_projection(
    session: AsyncSession,
    *,
    rows: Sequence[EventLogRow],
    account_id: UUID,
    environment: str,
    projector_version: str,
) -> tuple[
    dict[str, int],
    dict[str, str],
    dict[str, Decimal],
    dict[str, Decimal],
]:
    """Run the production projector against an event-only temporary schema.

    The connection has no access to runtime projection relations because its
    temporary tables shadow them.  Only the caller's verified event rows are
    copied into the temporary ``event_log`` before the normal deserializer and
    ``rebuild_snapshot_from_log`` projector are invoked.
    """
    projector_type = _projector_implementation(projector_version)
    source_connection = await session.connection()
    async with (
        source_connection.engine.connect() as connection,
        connection.begin(),
        AsyncSession(bind=connection, expire_on_commit=False) as replay_session,
    ):
        for table_name in _TEMPORARY_TABLE_NAMES:
            await replay_session.execute(text(
                "CREATE TEMPORARY TABLE "
                f"{table_name} (LIKE {table_name} INCLUDING ALL) ON COMMIT DROP"
            ))
        # LIKE copies the source serial default. Snapshot inserts omit id, so
        # give the temporary table its own identity sequence instead of using
        # (or requiring write privileges on) the source sequence. PostgreSQL
        # drops the owned temporary sequence with the table at commit.
        await replay_session.execute(text(
            "ALTER TABLE pg_temp.reconcile_observation "
            "ALTER COLUMN id DROP DEFAULT, "
            "ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY"
        ))
        if rows:
            await replay_session.execute(
                insert(EventLogRow),
                [
                    {
                        column.key: getattr(row, column.key)
                        for column in EventLogRow.__table__.columns
                    }
                    for row in rows
                ],
            )
        await replay_session.flush()
        await projector_type(
            deployment_environment=environment,
            event_only_replay=True,
        ).rebuild_snapshot_from_log(
            replay_session,
            account_id=str(account_id),
            deployment_environment=environment,
        )
        row_counts, content_hashes, offer_exposure, credit_exposure = await _projection_evidence(
            replay_session, account_id=account_id, environment=environment,
        )
    return row_counts, content_hashes, offer_exposure, credit_exposure


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
    _projector_implementation(projector_version)
    previous_seq: int | None = None
    event_ids: list[UUID] = []
    canonical_rows: list[dict[str, object]] = []
    for row in rows:
        if row.exchange_account_id != account_id or row.deployment_environment != environment:
            raise ReplayVerificationError("event stream scope mismatch")
        if row.event_seq is None:
            raise ReplayVerificationError("event stream has missing event sequence")
        if row.event_seq <= 0:
            raise ReplayVerificationError("event stream has non-positive event sequence")
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
        canonical_rows.append(canonical_event_record(row))
    event_hash = canonical_event_hash(rows)
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


async def _diagnostic_projection_evidence(
    session: AsyncSession, *, account_id: UUID, environment: str
) -> tuple[dict[str, int], dict[str, str]]:
    """Read the old runtime projection only after event replay succeeds."""
    row_counts, content_hashes, _, _ = await _projection_evidence(
        session, account_id=account_id, environment=environment
    )
    return row_counts, content_hashes


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
    (
        replayed_row_counts,
        replayed_content_hashes,
        replayed_offer_exposure,
        replayed_credit_exposure,
    ) = await _replay_into_empty_temporary_projection(
        session,
        rows=rows,
        account_id=account_id,
        environment=environment,
        projector_version=projector_version,
    )
    old_row_counts, old_content_hashes = await _diagnostic_projection_evidence(
        session, account_id=account_id, environment=environment,
    )
    return ReplayReport(
        **{
            **asdict(report),
            "row_counts": {"event_log": len(rows), **replayed_row_counts},
            "content_hashes": {"event_log": report.event_hash, **replayed_content_hashes},
            "diagnostic_old_row_counts": old_row_counts,
            "diagnostic_old_content_hashes": old_content_hashes,
            "diagnostic_diff": _diagnostic_diff(
                old_row_counts=old_row_counts,
                old_content_hashes=old_content_hashes,
                replayed_row_counts=replayed_row_counts,
                replayed_content_hashes=replayed_content_hashes,
            ),
            "replayed_offer_exposure_by_symbol": replayed_offer_exposure,
            "replayed_credit_exposure_by_symbol": replayed_credit_exposure,
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
