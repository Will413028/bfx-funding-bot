"""Read-only verification of caller-captured events using the production projector."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from uuid import UUID

from sqlalchemy import JSON, Text, cast, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.canonical import (
    canonical_event_hash,
    canonical_event_record,
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
    ProjectionHeadRow,
    ReconcileObservationRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import DEFAULT_PROJECTOR_VERSION
from bfx_funding_bot.modules.execution.projection_cutover.codec import JSON_NULL
from bfx_funding_bot.modules.execution.projection_cutover.diagnostics import RowCollector
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)

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
    diagnostic_collector: RowCollector | None = None,
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
    source_rows = (
        await _archive_projection_rows(session, account_id=account_id, environment=environment)
        if diagnostic_collector is not None else {}
    )
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
        if diagnostic_collector is not None:
            rebuilt_rows = await _archive_projection_rows(
                replay_session, account_id=account_id, environment=environment,
            )
            for name, model in _TEMPORARY_PROJECTION_MODELS:
                diagnostic_collector(
                    name, source_rows[name], rebuilt_rows[name],
                    key_columns=tuple(column.key for column in model.__table__.primary_key),  # type: ignore[attr-defined]
                )
    return row_counts, content_hashes, offer_exposure, credit_exposure


async def _archive_projection_rows(
    session: AsyncSession, *, account_id: UUID, environment: str,
) -> dict[str, list[dict[str, object]]]:
    """Copy every physical column on the owning connection, without hash exclusions."""
    result: dict[str, list[dict[str, object]]] = {}
    for name, model in _TEMPORARY_PROJECTION_MODELS:
        table = model.__table__  # type: ignore[attr-defined]
        json_names = {column.name for column in table.c if isinstance(column.type, JSON)}
        columns = [
            cast(column, Text).label(column.name) if column.name in json_names else column
            for column in table.c
        ]
        rows = await session.execute(select(*columns).where(
            table.c.exchange_account_id == account_id,
            table.c.deployment_environment == environment,
        ))
        values = [dict(row) for row in rows.mappings()]
        for value in values:
            for column in json_names:
                if value[column] is not None:
                    decoded = json.loads(value[column], parse_float=Decimal)
                    value[column] = JSON_NULL if decoded is None else decoded
        result[name] = values
    return result


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


async def replay_captured_rows(
    session: AsyncSession,
    *,
    rows: Sequence[EventLogRow],
    account_id: UUID,
    environment: str,
    projector_version: str = DEFAULT_PROJECTOR_VERSION,
    expected_event_hash: str | None = None,
    diagnostic_collector: RowCollector | None = None,
) -> ReplayReport:
    """Verify captured rows and rebuild on an isolated temporary connection.

    Rows may include the caller's flushed, uncommitted events. Never reread
    public event_log to recover them, or commit/rollback the source transaction.
    The default path supports READ COMMITTED. A full-row collector requires the
    caller to capture events within an existing REPEATABLE READ/serializable
    transaction so events and source diagnostics share one MVCC snapshot.
    """
    for row in rows:
        try:
            deserialize_stored_event(row)
        except (TypeError, ValueError) as exc:
            raise ReplayVerificationError(
                f"missing or invalid historical upcaster at seq={row.event_seq}: {exc}"
            ) from exc
    report = replay_event_log(
        rows, account_id=account_id, environment=environment,
        projector_version=projector_version, expected_event_hash=expected_event_hash,
    )
    if diagnostic_collector is not None:
        isolation = await session.scalar(text("SHOW transaction_isolation"))
        if isolation not in {"repeatable read", "serializable"}:
            raise ReplayVerificationError("field diagnostics require a consistent source snapshot")
    row_counts, content_hashes, offers, credits = await _replay_into_empty_temporary_projection(
        session, rows=rows, account_id=account_id, environment=environment,
        projector_version=projector_version, diagnostic_collector=diagnostic_collector,
    )
    return replace(
        report,
        row_counts={**report.row_counts, **row_counts},
        content_hashes={**report.content_hashes, **content_hashes},
        replayed_offer_exposure_by_symbol=offers,
        replayed_credit_exposure_by_symbol=credits,
    )
