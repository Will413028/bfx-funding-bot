"""Transaction-owned archive capture and SELECT-only verification.

Capture requires quiescent projection writers and the existing account lock.
The caller owns commit/rollback. Source and archive cursors hold at most one
256-row batch; PostgreSQL sorts encoded keys using the archive primary index.
Runtime must never import operator scripts or consume this archive for replay.
"""

import hashlib
import json
from decimal import Decimal
from functools import partial
from typing import Any
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import JSON, MetaData, Table, Text, cast, func, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_record
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter

from .codec import FORMAT_VERSION, decode_row, encode_row
from .contracts import ArchiveManifest, Scope, StreamIdentity
from .manifest import (
    TABLE_NAMES,
    decode_manifest,
    encode_manifest,
    seal_manifest,
    validate_manifest,
)
from .tables import ArchiveRow, ArchiveRun

_BATCH = 256


def _describe(connection: Connection, name: str) -> tuple[Table, list[dict[str, Any]], list[str]]:
    # Reflect actual PG columns, including unmapped historical fields. Explicit
    # public qualification prevents a temporary projector table shadowing them.
    inspector = inspect(connection)
    table = Table(name, MetaData(), schema="public", autoload_with=connection, resolve_fks=False)
    schema = [
        {
            "name": c["name"],
            "nullable": c["nullable"],
            "default": c["default"],
            "type": str(c["type"].compile(dialect=connection.dialect)),
            "identity": c.get("identity"),
            "computed": c.get("computed"),
        }
        for c in inspector.get_columns(name, schema="public")
    ]
    keys = inspector.get_pk_constraint(name, schema="public")["constrained_columns"]
    if not keys or not {"exchange_account_id", "deployment_environment"} <= set(table.c.keys()):
        raise ValueError("archive source lacks key/scope")
    return table, schema, keys


async def _stream_identity(session: AsyncSession, scope: Scope) -> StreamIdentity:
    # Incremental byte-for-byte equivalent of canonical_event_hash's JSON list.
    digest = hashlib.sha256(b"[")
    count = head = 0
    # Explicit public alias: no operator temporary relation can shadow event_log.
    table = Table("event_log", MetaData(), schema="public")
    statement = (
        select(EventLogRow)
        .from_statement(
            text(
                f"SELECT * FROM {table.fullname} WHERE exchange_account_id=:account "
                "AND deployment_environment=:environment ORDER BY event_seq"
            )
        )
        .params(account=scope.account_id, environment=scope.environment)
    )
    result = await session.stream_scalars(statement.execution_options(yield_per=_BATCH))
    try:
        async for row in result:
            if row.event_seq <= head:
                raise ValueError("non-increasing event stream")
            if count:
                digest.update(b",")
            digest.update(
                json.dumps(
                    canonical_event_record(row),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    default=str,
                ).encode()
            )
            count += 1
            head = row.event_seq
    finally:
        await result.close()
    digest.update(b"]")
    return StreamIdentity(count, head, digest.hexdigest())


async def _table_digest(
    session: AsyncSession, *, run_id: UUID, scope: Scope, entry: dict[str, Any]
) -> tuple[int, str]:
    table = type_cast(Table, ArchiveRow.__table__)
    query = (
        select(table)
        .where(table.c.run_id == run_id, table.c.table_name == entry["name"])
        .order_by(table.c.row_key)
    )
    result = await session.stream(query.execution_options(yield_per=_BATCH))
    digest = hashlib.sha256()
    count = 0
    previous: bytes | None = None
    try:
        async for row in result.mappings():
            payload, key = row["encoded_payload"], row["row_key"]
            values = decode_row(payload)
            decoded_key = decode_row(key)
            if (
                encode_row(values) != payload
                or encode_row(decoded_key) != key
                or set(values) != {c["name"] for c in entry["schema"]}
                or set(decoded_key) != set(entry["key_columns"])
                or any(values.get(k) is None for k in entry["key_columns"])
                or encode_row({k: values[k] for k in entry["key_columns"]}) != key
                or values.get("exchange_account_id") != scope.account_id
                or values.get("deployment_environment") != scope.environment
                or (previous is not None and key <= previous)
                or hashlib.sha256(payload).hexdigest() != row["row_digest"]
            ):
                raise ValueError("archive row identity/content mismatch")
            previous = key
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            count += 1
    finally:
        await result.close()
    return count, digest.hexdigest()


async def capture_archive(
    session: AsyncSession, *, scope: Scope, run_id: UUID, image_digest: str, projector_version: str
) -> ArchiveManifest:
    if not isinstance(scope.account_id, UUID) or scope.environment not in {"prod", "shadow", "ci"}:
        raise ValueError("invalid archive scope")
    connection = await session.connection()
    if connection.dialect.name != "postgresql":
        raise ValueError("archive requires PostgreSQL")
    # A pre-existing repeatable-read snapshot can predate acquisition of the
    # writer lock. Refuse it rather than silently capture stale event identity.
    if await connection.get_isolation_level() != "READ COMMITTED":
        raise ValueError("capture requires READ COMMITTED under the writer lock")
    await AccountEventWriter(
        store=PostgresEventStore(deployment_environment=scope.environment)
    ).acquire_lock(
        session,
        account_id=scope.account_id,
    )
    await session.flush()
    if not await session.scalar(
        text("SELECT EXISTS (SELECT 1 FROM public.exchange_accounts WHERE id=:id)"),
        {"id": scope.account_id},
    ):
        raise ValueError("archive account missing")
    runs = type_cast(Table, ArchiveRun.__table__)
    if await session.scalar(select(runs.c.run_id).where(runs.c.run_id == run_id)):
        raise ValueError("archive run already exists")
    heads = tuple(
        (
            await session.execute(
                text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
            )
        ).scalars()
    )
    if heads != ("f8c2d4e6a901",):
        raise ValueError("archive migration not ready")
    stream = await _stream_identity(session, scope)
    entries: list[dict[str, object]] = []
    await session.execute(
        runs.insert().values(
            run_id=run_id,
            exchange_account_id=scope.account_id,
            deployment_environment=scope.environment,
            manifest=b"",
            complete=False,
        )
    )
    for name in TABLE_NAMES:
        table, schema, keys = await connection.run_sync(partial(_describe, name=name))
        # JSONB text avoids the DB driver's float conversion losing numeric
        # digits. Decimal retains the database's actual JSON numeric values.
        json_names = {c.name for c in table.c if isinstance(c.type, JSON)}
        columns = [cast(c, Text).label(c.name) if c.name in json_names else c for c in table.c]
        query = select(*columns).where(
            table.c.exchange_account_id == scope.account_id,
            table.c.deployment_environment == scope.environment,
        )
        result = await session.stream(query.execution_options(yield_per=_BATCH))
        source_count = 0
        try:
            async for batch in result.mappings().partitions(_BATCH):
                inserts = []
                for row in batch:
                    values = dict(row)
                    for column in json_names:
                        if values[column] is not None:
                            values[column] = json.loads(values[column], parse_float=Decimal)
                    payload = encode_row(values)
                    key = encode_row({k: values[k] for k in keys})
                    if any(values[k] is None for k in keys) or decode_row(payload) != values:
                        raise ValueError("archive row failed round-trip/key validation")
                    inserts.append(
                        {
                            "run_id": run_id,
                            "table_name": name,
                            "row_key": key,
                            "encoded_payload": payload,
                            "row_digest": hashlib.sha256(payload).hexdigest(),
                        }
                    )
                await session.execute(type_cast(Table, ArchiveRow.__table__).insert(), inserts)
                source_count += len(inserts)
        finally:
            await result.close()
        entry = {"name": name, "schema": schema, "key_columns": keys}
        count, digest = await _table_digest(session, run_id=run_id, scope=scope, entry=entry)
        if count != source_count:
            raise ValueError("archive capture count mismatch")
        entries.append({**entry, "count": count, "digest": digest})
    manifest = seal_manifest(
        ArchiveManifest(
            run_id,
            scope,
            stream,
            FORMAT_VERSION,
            image_digest,
            heads,
            projector_version,
            tuple(entries),
            "",
        )
    )
    await session.execute(
        update(runs)
        .where(runs.c.run_id == run_id)
        .values(
            manifest=encode_manifest(manifest),
            complete=True,
        )
    )
    await verify_archive(session, expected=manifest)
    return manifest


async def verify_archive(session: AsyncSession, *, expected: ArchiveManifest) -> None:
    # Copy the mutable nested dictionaries in Task 1's frozen data carrier.
    validate_manifest(expected)
    expected = decode_manifest(encode_manifest(expected))
    with session.no_autoflush:
        runs = type_cast(Table, ArchiveRun.__table__)
        row = (
            (await session.execute(select(runs).where(runs.c.run_id == expected.run_id)))
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or not row["complete"]
            or row["exchange_account_id"] != expected.scope.account_id
            or row["deployment_environment"] != expected.scope.environment
            or decode_manifest(row["manifest"]) != expected
        ):
            raise ValueError("archive run/independent manifest mismatch")
        table = ArchiveRow.__table__
        counts = {
            str(name): int(count)
            for name, count in (
                await session.execute(
                    select(table.c.table_name, func.count())
                    .where(
                        table.c.run_id == expected.run_id,
                    )
                    .group_by(table.c.table_name)
                    # Nine distinct names necessarily violate the eight-table
                    # allowlist; corrupted archives cannot force an unbounded
                    # client-side allocation before that rejection.
                    .limit(len(TABLE_NAMES) + 1)
                )
            ).all()
        }
        if set(counts) - set(TABLE_NAMES):
            raise ValueError("archive contains unexpected table rows")
        connection = await session.connection()
        for entry in expected.tables:
            _, schema, keys = await connection.run_sync(partial(_describe, name=str(entry["name"])))
            if schema != entry["schema"] or keys != entry["key_columns"]:
                raise ValueError("archive schema/key mismatch")
            count, digest = await _table_digest(
                session, run_id=expected.run_id, scope=expected.scope, entry=entry
            )
            if (
                count != entry["count"]
                or counts.get(str(entry["name"]), 0) != count
                or digest != entry["digest"]
            ):
                raise ValueError("archive count/content digest mismatch")


async def audit_runtime_roles(session: AsyncSession, *, role_names: tuple[str, ...]) -> None:
    """Fail closed for runtime roles, including reachable SET ROLE privileges.

    SELECT-only preflight; never changes a real role. Callers must supply their full
    runtime role inventory. A superuser bot is a production blocker requiring a
    separately reviewed operator correction, even if PUBLIC has no archive grants.
    """
    if not role_names:
        raise ValueError("runtime role inventory required")
    for name in role_names:
        roles = (
            (
                await session.execute(
                    text("""
            WITH RECURSIVE reachable AS (
              SELECT oid FROM pg_roles WHERE rolname=:name
              UNION
              SELECT m.roleid FROM pg_auth_members m JOIN reachable r ON m.member=r.oid
            ) SELECT p.* FROM pg_roles p JOIN reachable r ON r.oid=p.oid
        """),
                    {"name": name},
                )
            )
            .mappings()
            .all()
        )
        if not roles:
            raise ValueError("runtime role missing")
        for role in roles:
            if any(
                role[k]
                for k in (
                    "rolsuper",
                    "rolcreaterole",
                    "rolcreatedb",
                    "rolreplication",
                    "rolbypassrls",
                )
            ):
                raise ValueError("unsafe privileged runtime role")
            unsafe = await session.scalar(
                text("""
                SELECT
                  NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='projection_audit')
                  OR (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='projection_audit' AND c.relkind IN ('r','p')
                    AND c.relname IN ('runs','rows','receipts')) <> 3
                  OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='projection_audit'
                    AND (nspowner=:oid OR has_schema_privilege(:oid, oid, 'CREATE')))
                  OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='projection_audit' AND c.relkind IN ('r','p') AND (
                      c.relowner=:oid OR has_table_privilege(:oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                      OR has_any_column_privilege(:oid,c.oid,'INSERT,UPDATE,REFERENCES')))
            """),
                {"oid": role["oid"]},
            )
            if unsafe:
                raise ValueError("runtime role has archive write/ownership privileges")
