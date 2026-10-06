"""Record the rolling prefix hash of each event beside the append-only ledger."""
import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "c3f5a1d7e204"
down_revision = "b4e6f8a0c203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Beside the ledger, not on it. event_log is append-only and
    # guard_capital_event() enforces that for capital-bearing rows, so sealing
    # existing events with an UPDATE is refused for exactly the events capital
    # reads depend on. Inserting keeps the ledger immutable and the chain whole.
    op.create_table(
        "event_prefix_hashes",
        sa.Column("event_seq", sa.BigInteger(),
                  sa.ForeignKey("event_log.event_seq", ondelete="RESTRICT"), primary_key=True),
        sa.Column("exchange_account_id", sa.Uuid(), nullable=True),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("prefix_hash", sa.Text(), nullable=False),
    )
    op.create_index("idx_event_prefix_scope_seq", "event_prefix_hashes",
                    ["exchange_account_id", "deployment_environment", "event_seq"])

    # Seal every existing event here. A reader treats a missing link as unproven
    # rather than absent, and the writer refuses to extend a chain whose
    # predecessor has none, so a partial backfill would block the next append.
    # The hash is the writer's record contract (event_store.canonical at this
    # revision), carried here verbatim so the migration depends on no live module:
    # the event store's ORM moved to legacy_archive later (c2d3e4f5a6b7), and the
    # code goes with the event store. tests/integration/test_event_prefix_hash_migration.py
    # pins this copy to the writer's chain.
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(*_EVENT_LOG.c).order_by(
            _EVENT_LOG.c.exchange_account_id,
            _EVENT_LOG.c.deployment_environment,
            _EVENT_LOG.c.event_seq,
        )
    ).all()
    previous_key = None
    previous_hash = GENESIS_PREFIX_HASH
    for row in rows:
        key = (row.exchange_account_id, row.deployment_environment)
        if key != previous_key:
            previous_key, previous_hash = key, GENESIS_PREFIX_HASH
        previous_hash = rolling_prefix_hash(previous_hash, row)
        bind.execute(_PREFIX_HASHES.insert().values(
            event_seq=row.event_seq,
            exchange_account_id=row.exchange_account_id,
            deployment_environment=row.deployment_environment,
            prefix_hash=previous_hash,
        ))


# -- the writer's prefix-hash contract at this revision (event_store.canonical,
# serialization.stored_event_identity, projector.derive_v2_event_id), verbatim -----------

_EVENT_LOG = sa.table(
    "event_log",
    sa.column("event_seq", sa.BigInteger()),
    sa.column("account_id", sa.Text()),
    sa.column("exchange_account_id", sa.Uuid()),
    sa.column("deployment_environment", sa.Text()),
    sa.column("event_type", sa.Text()),
    sa.column("cid", sa.BigInteger()),
    sa.column("venue_offer_id", sa.Text()),
    sa.column("venue_seq", sa.BigInteger()),
    sa.column("event_id", sa.Uuid()),
    sa.column("schema_version", sa.Integer()),
    sa.column("payload", JSONB()),
    sa.column("occurred_at_ms", sa.BigInteger()),
)
_PREFIX_HASHES = sa.table(
    "event_prefix_hashes",
    sa.column("event_seq", sa.BigInteger()),
    sa.column("exchange_account_id", sa.Uuid()),
    sa.column("deployment_environment", sa.Text()),
    sa.column("prefix_hash", sa.Text()),
)
GENESIS_PREFIX_HASH = hashlib.sha256(b"bfx-event-prefix-v1").hexdigest()
_SCHEMA_VERSION = 3
_SUPPORTED_SCHEMA_VERSIONS = frozenset({2, _SCHEMA_VERSION})
_V2_EVENT_NAMESPACE = uuid5(NAMESPACE_URL, "bfx-funding-bot/event-log/v2")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=str)


def _event_id(row: Any) -> UUID:
    payload = row.payload
    if not isinstance(payload, dict):
        raise TypeError("stored event payload must be an object")
    version = payload.get("__schema_version__")
    if row.schema_version is not None:
        if row.schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported stored event schema version: {row.schema_version!r}")
        expected = 2 if version is None else version
        if row.schema_version != expected:
            raise ValueError(
                "stored event schema_version does not match payload: "
                f"row={row.schema_version!r}, payload={version!r}")
    if version == _SCHEMA_VERSION:
        raw_event_id = payload.get("event_id")
        if raw_event_id is None:
            raise ValueError("schema-v3 event payload requires event_id")
        try:
            event_id = UUID(str(raw_event_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("schema-v3 event payload has invalid event_id") from exc
        if row.event_id is not None and row.event_id != event_id:
            raise ValueError("stored event_id does not match schema-v3 payload")
        return event_id
    if version is not None and version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported event schema version: {version!r}")
    if row.event_seq is None:
        raise TypeError("historical event identity requires persistent event_seq")
    name = json.dumps(
        [int(row.event_seq), str(row.account_id), str(row.deployment_environment),
         str(row.event_type), int(row.occurred_at_ms), _json(payload)],
        separators=(",", ":"), ensure_ascii=True,
    )
    return uuid5(_V2_EVENT_NAMESPACE, name)


def rolling_prefix_hash(previous: str, row: Any) -> str:
    if row.event_seq is None:
        raise ValueError("event stream has missing event sequence")
    record = {
        "event_seq": row.event_seq,
        "event_id": str(_event_id(row)),
        "schema_version": row.schema_version,
        "event_type": row.event_type,
        "cid": row.cid,
        "venue_offer_id": row.venue_offer_id,
        "venue_seq": row.venue_seq,
        "payload": row.payload,
        "occurred_at_ms": row.occurred_at_ms,
    }
    return hashlib.sha256(
        previous.encode("ascii") + b"\x00" + _json(record).encode()).hexdigest()


def downgrade() -> None:
    op.drop_index("idx_event_prefix_scope_seq", table_name="event_prefix_hashes")
    op.drop_table("event_prefix_hashes")
