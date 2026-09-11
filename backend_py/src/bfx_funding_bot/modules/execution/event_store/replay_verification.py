"""Read-only verification of caller-captured events using the production projector."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
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
from bfx_funding_bot.modules.execution.projection_cutover.codec import (
    JSON_NULL,
    decode_row,
    encode_row,
)
from bfx_funding_bot.modules.execution.projection_cutover.diagnostics import (
    DifferenceSink,
    RowCollector,
    compare_sorted_rows,
)
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
_PROJECTION_STREAM_BATCH = 256
_PROJECTION_SPOOL_TABLES = {"source": "source_rows", "rebuilt": "rebuilt_rows"}
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
    streaming: bool = False,
) -> tuple[dict[str, int], dict[str, str], dict[str, Decimal], dict[str, Decimal]]:
    """Hash scoped projection rows without retaining or reporting their contents."""
    if streaming:
        return await _stream_projection_evidence(
            session, account_id=account_id, environment=environment,
        )
    row_counts: dict[str, int] = {}
    content_hashes: dict[str, str] = {}
    for name, model in _TEMPORARY_PROJECTION_MODELS:
        rows = list(await session.scalars(select(model).where(
            model.exchange_account_id == account_id,  # type: ignore[attr-defined]
            model.deployment_environment == environment,  # type: ignore[attr-defined]
        ).execution_options(populate_existing=True)))
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
    ).execution_options(populate_existing=True)):
        offer_exposure[offer_row.symbol] = (
            offer_exposure.get(offer_row.symbol, Decimal("0"))
            + Decimal(str(offer_row.amount_remaining))
        )
    credit_exposure: dict[str, Decimal] = {}
    for credit_row in await session.scalars(select(VenueCreditStateRow).where(
        VenueCreditStateRow.exchange_account_id == account_id,
        VenueCreditStateRow.deployment_environment == environment,
        VenueCreditStateRow.is_terminal.is_(False),
    ).execution_options(populate_existing=True)):
        credit_exposure[credit_row.symbol] = (
            credit_exposure.get(credit_row.symbol, Decimal("0"))
            + Decimal(str(credit_row.amount))
        )
    return row_counts, content_hashes, offer_exposure, credit_exposure


@contextmanager
def _projection_hash_spool() -> Iterator[sqlite3.Connection]:
    """Provide a private disk-backed spool for canonical projection hashes."""
    with TemporaryDirectory(prefix="bfx-projection-hash-") as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        path = root / "projection.sqlite"
        path.touch(mode=0o600, exist_ok=False)
        os.chmod(path, 0o600)
        with closing(sqlite3.connect(path)) as spool:
            spool.execute("PRAGMA cache_size=-512")
            spool.execute("PRAGMA temp_store=FILE")
            spool.execute(
                "CREATE TABLE canonical_rows("
                "sort_key TEXT NOT NULL, sequence INTEGER NOT NULL, payload BLOB NOT NULL,"
                "PRIMARY KEY(sort_key, sequence)) WITHOUT ROWID"
            )
            spool.commit()
            yield spool


def _canonical_projection_bytes(row: Mapping[str, object]) -> bytes:
    """Encode one canonical row exactly as ``projection_content_hash`` does."""
    return json.dumps(
        row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str,
    ).encode()


async def _stream_projection_table_hash(
    session: AsyncSession,
    *,
    model: type[object],
    account_id: UUID,
    environment: str,
) -> tuple[int, str]:
    """Return the existing projection hash using a bounded external sort."""
    table = model.__table__  # type: ignore[attr-defined]  # SQLAlchemy mapped row.
    statement = select(model).where(
        table.c.exchange_account_id == account_id,
        table.c.deployment_environment == environment,
    ).execution_options(populate_existing=True, yield_per=_PROJECTION_STREAM_BATCH)
    with _projection_hash_spool() as spool:
        result = await session.stream_scalars(statement)
        count = 0
        try:
            async for row in result:
                canonical = _canonical_projection_row(row)
                payload = _canonical_projection_bytes(canonical)
                sort_key = hashlib.sha256(b"[" + payload + b"]").hexdigest()
                spool.execute(
                    "INSERT INTO canonical_rows(sort_key, sequence, payload) VALUES (?, ?, ?)",
                    (sort_key, count, payload),
                )
                count += 1
        finally:
            await result.close()
        spool.commit()

        digest = hashlib.sha256(b"[")
        cursor = spool.execute(
            "SELECT payload FROM canonical_rows ORDER BY sort_key, sequence"
        )
        try:
            first = True
            for (payload,) in cursor:
                if not first:
                    digest.update(b",")
                digest.update(bytes(payload))
                first = False
        finally:
            cursor.close()
        digest.update(b"]")
    return count, digest.hexdigest()


async def _stream_projection_evidence(
    session: AsyncSession, *, account_id: UUID, environment: str,
) -> tuple[dict[str, int], dict[str, str], dict[str, Decimal], dict[str, Decimal]]:
    """Compute projection evidence without materializing physical rows in Python."""
    row_counts: dict[str, int] = {}
    content_hashes: dict[str, str] = {}
    for name, model in _TEMPORARY_PROJECTION_MODELS:
        row_counts[name], content_hashes[name] = await _stream_projection_table_hash(
            session, model=model, account_id=account_id, environment=environment,
        )

    offer_exposure: dict[str, Decimal] = {}
    offer_result = await session.stream_scalars(select(VenueOfferStateRow).where(
        VenueOfferStateRow.exchange_account_id == account_id,
        VenueOfferStateRow.deployment_environment == environment,
        VenueOfferStateRow.is_terminal.is_(False),
    ).execution_options(populate_existing=True, yield_per=_PROJECTION_STREAM_BATCH))
    try:
        async for offer_row in offer_result:
            offer_exposure[offer_row.symbol] = (
                offer_exposure.get(offer_row.symbol, Decimal("0"))
                + Decimal(str(offer_row.amount_remaining))
            )
    finally:
        await offer_result.close()

    credit_exposure: dict[str, Decimal] = {}
    credit_result = await session.stream_scalars(select(VenueCreditStateRow).where(
        VenueCreditStateRow.exchange_account_id == account_id,
        VenueCreditStateRow.deployment_environment == environment,
        VenueCreditStateRow.is_terminal.is_(False),
    ).execution_options(populate_existing=True, yield_per=_PROJECTION_STREAM_BATCH))
    try:
        async for credit_row in credit_result:
            credit_exposure[credit_row.symbol] = (
                credit_exposure.get(credit_row.symbol, Decimal("0"))
                + Decimal(str(credit_row.amount))
            )
    finally:
        await credit_result.close()
    return row_counts, content_hashes, offer_exposure, credit_exposure


@contextmanager
def _projection_spool() -> Iterator[sqlite3.Connection]:
    """Provide a private disk-backed spool for one projection table."""
    with TemporaryDirectory(prefix="bfx-projection-diagnostic-") as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        path = root / "projection.sqlite"
        path.touch(mode=0o600, exist_ok=False)
        os.chmod(path, 0o600)
        with closing(sqlite3.connect(path)) as spool:
            spool.execute("PRAGMA cache_size=-512")
            spool.execute("PRAGMA temp_store=FILE")
            spool.execute(
                "CREATE TABLE source_rows("
                "row_key BLOB PRIMARY KEY, payload BLOB NOT NULL"
                ") WITHOUT ROWID"
            )
            spool.execute(
                "CREATE TABLE rebuilt_rows("
                "row_key BLOB PRIMARY KEY, payload BLOB NOT NULL"
                ") WITHOUT ROWID"
            )
            spool.commit()
            yield spool


def _decode_projection_json(value: object) -> object:
    decoded = json.loads(value, parse_float=Decimal) if isinstance(value, (bytes, str)) else value
    return JSON_NULL if decoded is None else decoded


def _projection_values(
    row: Mapping[str, object], *, json_names: set[str]
) -> dict[str, object]:
    values = dict(row)
    for column in json_names:
        if values[column] is not None:
            values[column] = _decode_projection_json(values[column])
    return values


async def _spool_projection_rows(
    session: AsyncSession,
    *,
    model: type[object],
    account_id: UUID,
    environment: str,
    spool: sqlite3.Connection,
    target: str,
) -> int:
    table = model.__table__  # type: ignore[attr-defined]  # SQLAlchemy mapped row.
    key_columns = tuple(column.key for column in table.primary_key)
    json_names = {column.name for column in table.c if isinstance(column.type, JSON)}
    columns = [
        cast(column, Text).label(column.name) if column.name in json_names else column
        for column in table.c
    ]
    statement = select(*columns).where(
        table.c.exchange_account_id == account_id,
        table.c.deployment_environment == environment,
    ).execution_options(
        populate_existing=True,
        yield_per=_PROJECTION_STREAM_BATCH,
    )
    result = await session.stream(statement)
    count = 0
    try:
        async for raw_row in result.mappings():
            values = _projection_values(
                {str(key): value for key, value in raw_row.items()},
                json_names=json_names,
            )
            if any(column not in values for column in key_columns):
                raise ReplayVerificationError("missing archive row key")
            if any(values[column] is None for column in key_columns):
                raise ReplayVerificationError("null archive row key")
            payload = encode_row(values)
            key = encode_row({column: values[column] for column in key_columns})
            try:
                spool.execute(
                    f"INSERT INTO {target}(row_key, payload) VALUES (?, ?)",
                    (key, payload),
                )
            except sqlite3.IntegrityError:
                raise ReplayVerificationError("duplicate archive row key") from None
            count += 1
    finally:
        await result.close()
    spool.commit()
    return count


def _spool_cursor(
    spool: sqlite3.Connection, *, side: str
) -> Iterator[Mapping[str, object]]:
    try:
        table = _PROJECTION_SPOOL_TABLES[side]
    except KeyError:
        raise ValueError("invalid projection spool side") from None
    cursor = spool.execute(f"SELECT payload FROM {table} ORDER BY row_key")
    try:
        for (payload,) in cursor:
            yield decode_row(bytes(payload))
    finally:
        cursor.close()


def _spool_facts(
    spool: sqlite3.Connection, *, side: str
) -> tuple[int, str]:
    try:
        table = _PROJECTION_SPOOL_TABLES[side]
    except KeyError:
        raise ValueError("invalid projection spool side") from None
    digest = hashlib.sha256()
    count = 0
    cursor = spool.execute(f"SELECT payload FROM {table} ORDER BY row_key")
    try:
        for (payload,) in cursor:
            encoded = bytes(payload)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
    finally:
        cursor.close()
    return count, digest.hexdigest()


async def _stream_projection_differences(
    source_session: AsyncSession,
    rebuilt_session: AsyncSession,
    *,
    account_id: UUID,
    environment: str,
    difference_sink: DifferenceSink,
) -> None:
    """Spool and merge one physical projection table at a time."""
    models = sorted(_TEMPORARY_PROJECTION_MODELS, key=lambda item: item[0])
    for name, model in models:
        table = model.__table__  # type: ignore[attr-defined]  # SQLAlchemy mapped row.
        key_columns = tuple(column.key for column in table.primary_key)
        difference_sink.begin_table(name, key_columns)
        with _projection_spool() as spool:
            source_count = await _spool_projection_rows(
                source_session,
                model=model,
                account_id=account_id,
                environment=environment,
                spool=spool,
                target=_PROJECTION_SPOOL_TABLES["source"],
            )
            rebuilt_count = await _spool_projection_rows(
                rebuilt_session,
                model=model,
                account_id=account_id,
                environment=environment,
                spool=spool,
                target=_PROJECTION_SPOOL_TABLES["rebuilt"],
            )
            source_facts = _spool_facts(spool, side="source")
            rebuilt_facts = _spool_facts(spool, side="rebuilt")
            if source_facts[0] != source_count or rebuilt_facts[0] != rebuilt_count:
                raise ReplayVerificationError("projection spool count mismatch")
            for difference in compare_sorted_rows(
                _spool_cursor(spool, side="source"),
                _spool_cursor(spool, side="rebuilt"),
                key_columns=key_columns,
                table=name,
            ):
                difference_sink.append(difference)
            table_facts: Mapping[str, object] = {
                "name": name,
                "key_columns": list(key_columns),
                "count": source_facts[0],
                "digest": source_facts[1],
            }
        difference_sink.finish_table(table_facts)


async def _replay_into_empty_temporary_projection(
    session: AsyncSession,
    *,
    rows: Sequence[EventLogRow],
    account_id: UUID,
    environment: str,
    projector_version: str,
    diagnostic_collector: RowCollector | None = None,
    difference_sink: DifferenceSink | None = None,
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
    if diagnostic_collector is not None and difference_sink is not None:
        raise ReplayVerificationError("choose one projection diagnostic boundary")
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
        # A pooled connection may hold prepared SELECTs previously resolved to
        # public tables. Creating a shadow table alone need not reparse those
        # statements. An explicit local path change binds every cached plan to
        # the temporary projection; transaction exit restores the caller path.
        await replay_session.execute(text("SET LOCAL search_path TO pg_temp, public"))
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
            replay_session,
            account_id=account_id,
            environment=environment,
            streaming=difference_sink is not None,
        )
        if difference_sink is not None:
            await _stream_projection_differences(
                session,
                replay_session,
                account_id=account_id,
                environment=environment,
                difference_sink=difference_sink,
            )
        elif diagnostic_collector is not None:
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
    difference_sink: DifferenceSink | None = None,
    diagnostic_collector: RowCollector | None = None,
) -> ReplayReport:
    """Verify captured rows and rebuild on an isolated temporary connection.

    Rows may include the caller's flushed, uncommitted events. Never reread
    public event_log to recover them, or commit/rollback the source transaction.
    The default path supports READ COMMITTED. A diagnostic sink requires the
    caller to capture events within an existing REPEATABLE READ/serializable
    transaction so events and source diagnostics share one MVCC snapshot.
    """
    if difference_sink is not None and diagnostic_collector is not None:
        raise ReplayVerificationError("choose one projection diagnostic boundary")
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
    if difference_sink is not None or diagnostic_collector is not None:
        isolation = await session.scalar(text("SHOW transaction_isolation"))
        if isolation not in {"repeatable read", "serializable"}:
            raise ReplayVerificationError("field diagnostics require a consistent source snapshot")
    row_counts, content_hashes, offers, credits = await _replay_into_empty_temporary_projection(
        session, rows=rows, account_id=account_id, environment=environment,
        projector_version=projector_version,
        diagnostic_collector=diagnostic_collector,
        difference_sink=difference_sink,
    )
    return replace(
        report,
        row_counts={**report.row_counts, **row_counts},
        content_hashes={**report.content_hashes, **content_hashes},
        replayed_offer_exposure_by_symbol=offers,
        replayed_credit_exposure_by_symbol=credits,
    )
