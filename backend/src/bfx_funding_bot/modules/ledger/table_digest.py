"""Per-table count / ordered sha256 / watermark of the ledger tables (S1-4b).

One digest per table of ``LEDGER_TABLES`` plus ``capital_authority_epoch``, computed inside the
caller's REPEATABLE READ READ ONLY transaction, or from in-memory row dicts with the same
canonicalizer (``digest_rows``), so a seed tool can state the digest it expects before writing
and a reader can recompute it from the database afterwards: identical rows, identical bytes.

Canonical bytes (``TABLE_DIGEST_VERSION`` 1)
* sha256 over a header line (``bfx-ledger-table-digest/v1``, table name, canonical column
  names in order), then one line per row in the table's total order (``ORDER_BY``), each row a
  compact JSON-style array, one cell per canonical column, ``\\n`` terminated. An empty table is
  the header's sha256.
* Cells by column type: SQL NULL ``null``; boolean ``true``/``false``; integer decimal digits;
  uuid the lower-case hyphenated string; text a JSON string (UTF-8, not ASCII-escaped);
  Numeric a JSON string ``format(v, "f")`` WITHOUT ``normalize`` (F1 ruling: the venue's wire
  digits stay; ``Decimal("1.10")`` and ``Decimal("1.1")`` are different bytes, and so are two
  Numeric values that PostgreSQL stores with different scales); JSON the ledger's
  ``canonical_payload`` bytes (JSON ``null`` and SQL NULL both encode ``null``).
* The canonical columns are explicit per table (``CANONICAL_COLUMNS``, never ``SELECT *``);
  generated columns are derived from other canonical columns and are excluded. A test pins the
  lists against the ORM so a new column needs a conscious decision here.
* Order keys use only the row's own canonical columns, ending in the primary key, so the order
  is total and the in-memory path needs no joined data. Text keys sort by UTF-8 bytes
  (``COLLATE "C"`` in SQL), never by the database collation; uuid keys sort bytewise.

Mutable tables (clock, mirrors) are digests of one capture point only: later legitimate writes
change them (``TableDigest.mutable``). Everything else is append-only and comparable over time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import JSON as SA_JSON
from sqlalchemy import BigInteger, Boolean, Integer, Numeric, Select, Table, Text, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Uuid

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES, CapitalAuthorityEpochRow

TABLE_DIGEST_VERSION = 1
_DOMAIN = "bfx-ledger-table-digest/v1"
_STREAM_BATCH = 1000

type CellKind = Literal["uuid", "text", "int", "bool", "decimal", "json"]
type Row = Mapping[str, object]


class TableDigestRefused(RuntimeError):  # noqa: N818 - a refusal, like CapitalReadRefused
    """The digest cannot be computed here (wrong transaction mode)."""


class TableDigestRowInvalid(ValueError):  # noqa: N818 - a refusal of one caller-supplied row
    """A row does not match its table's canonical columns or cell types."""


# ORM columns that are NOT canonical, per table: the generated (derived) columns.
GENERATED_COLUMNS: dict[str, tuple[str, ...]] = {
    "ledger_observation": ("history_symbols", "first_page_counts"),
    "submission_attempt_journal": (
        "intended_amount",
        "match_rate",
        "match_period_days",
        "match_offer_type",
        "match_flags",
    ),
}

CANONICAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "capital_command_clock": (
        "exchange_account_id", "deployment_environment", "revision",
    ),
    "ledger_observation_query": (
        "query_id", "exchange_account_id", "deployment_environment", "query_revision",
        "started_at_ms", "start_revision",
    ),
    "ledger_observation": (
        "id", "query_id", "exchange_account_id", "deployment_environment", "schema_version",
        "query_finished_at_ms", "confirmation_finished_at_ms", "accept_revision",
        "wallets_complete", "offers_complete", "credits_complete", "loans_complete",
        "offer_history_complete", "credit_history_complete", "trades_complete",
        "trades_requested_start_ms", "trades_requested_end_ms", "offer_history_pages",
        "credit_history_pages", "history_requested_start_ms", "history_requested_end_ms",
        "history_oldest_mts_created", "history_newest_mts_created", "first_digest",
        "confirmation_digest", "accepted", "evidence",
    ),
    "ledger_observation_wallet": (
        "observation_id", "wallet_type", "currency", "available", "balance", "symbol",
    ),
    "ledger_observation_offer": (
        "id", "observation_id", "venue_offer_id", "symbol", "amount_original",
        "amount_remaining", "rate", "rate_observed", "period_days", "offer_type", "flags",
        "status", "mts_created", "mts_updated", "raw",
    ),
    "ledger_observation_credit": (
        "id", "observation_id", "venue_credit_id", "source_kind", "symbol", "amount", "rate",
        "period_days", "status", "flags", "mts_created", "mts_updated", "mts_opening", "raw",
    ),
    "ledger_observation_offer_history": (
        "id", "observation_id", "venue_offer_id", "symbol", "amount_original",
        "amount_remaining", "rate", "rate_observed", "period_days", "offer_type", "flags",
        "status", "mts_created", "mts_updated", "terminal_kind", "occurred_at_ms", "raw",
    ),
    "ledger_observation_credit_history": (
        "id", "observation_id", "venue_credit_id", "source_kind", "symbol", "amount", "rate",
        "period_days", "status", "flags", "mts_created", "mts_updated", "mts_opening",
        "terminal_kind", "occurred_at_ms", "raw",
    ),
    "ledger_observation_trade": (
        "observation_id", "trade_id", "symbol", "venue_offer_id", "amount", "rate",
        "period_days", "mts_create", "maker",
    ),
    "venue_offer_mirror": (
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "rate", "rate_observed", "period_days",
        "offer_type", "flags", "status", "mts_created", "mts_updated",
        "last_accepted_observation_id", "present_in_latest_accepted_snapshot",
        "terminal_evidence_id", "terminal_kind",
    ),
    "venue_credit_mirror": (
        "exchange_account_id", "deployment_environment", "venue_credit_id", "source_kind",
        "symbol", "amount", "rate", "period_days", "status", "flags", "mts_created",
        "mts_updated", "mts_opening", "last_accepted_observation_id",
        "present_in_latest_accepted_snapshot", "terminal_evidence_id", "terminal_kind",
    ),
    "submission_attempt_journal": (
        "attempt_id", "execution_decision_id", "exchange_account_id", "deployment_environment",
        "symbol", "cell_id", "attempt_seq", "normalized_payload", "payload_sha256", "basis_id",
        "policy_revision_id", "authorization_evidence", "seed_provenance", "started_at_ms",
    ),
    "transport_outcome_journal": (
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms", "evidence",
    ),
    "quarantine_opening": (
        "quarantine_id", "exchange_account_id", "deployment_environment", "symbol",
        "intended_amount", "opened_at_ms", "opened_revision", "evidence",
        "legacy_reconcile_event_seq", "source_attempt_id",
    ),
    "quarantine_member": (
        "quarantine_id", "source_kind", "venue_object_id", "observation_id", "amount_at_join",
    ),
    "execution_resolution_journal": (
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "observation_id", "actor_kind", "actor_id",
        "operator_request_id", "resolved_at_ms", "candidate_count", "reason", "evidence",
    ),
    "accepted_capital_basis": (
        "id", "exchange_account_id", "deployment_environment", "observation_id", "accepted",
        "accept_revision", "attempt_seq_high_water", "scope_block", "schema_version", "digest",
        "accepted_at_ms",
    ),
    "accepted_capital_basis_symbol": (
        "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits",
        "foreign_offers", "block", "conservation", "lent_unexplained", "foreign_executed",
        "fill_conflicts",
    ),
    "accepted_capital_basis_cell": (
        "basis_id", "symbol", "cell_id", "amount",
    ),
    "accepted_capital_basis_credit": (
        "basis_id", "source_kind", "venue_credit_id", "symbol", "amount", "period_days",
        "mts_opening", "attribution_basis",
    ),
    "accepted_capital_basis_credit_cell": (
        "basis_id", "source_kind", "venue_credit_id", "cell_id",
    ),
    "accepted_capital_basis_attempt": (
        "basis_id", "attempt_id", "symbol", "classification",
    ),
    "accepted_capital_basis_quarantine": (
        "basis_id", "quarantine_id",
    ),
    "capital_authority_epoch": (
        "epoch_seq", "authority", "set_at_ms", "actor", "reason", "evidence",
    ),
}

# Total order per table: the listed keys, then whatever primary-key columns they do not cover
# (pinned: every key is canonical and the primary key is contained).
ORDER_BY: dict[str, tuple[str, ...]] = {
    "capital_command_clock": ("exchange_account_id", "deployment_environment"),
    "ledger_observation_query": (
        "exchange_account_id",
        "deployment_environment",
        "query_revision",
        "query_id",
    ),
    "ledger_observation": ("exchange_account_id", "deployment_environment", "id"),
    "ledger_observation_wallet": ("observation_id", "wallet_type", "currency"),
    "ledger_observation_offer": ("observation_id", "id"),
    "ledger_observation_credit": ("observation_id", "id"),
    "ledger_observation_offer_history": ("observation_id", "id"),
    "ledger_observation_credit_history": ("observation_id", "id"),
    "ledger_observation_trade": ("observation_id", "trade_id"),
    "venue_offer_mirror": ("exchange_account_id", "deployment_environment", "venue_offer_id"),
    "venue_credit_mirror": (
        "exchange_account_id",
        "deployment_environment",
        "venue_credit_id",
        "source_kind",
    ),
    "submission_attempt_journal": (
        "exchange_account_id",
        "deployment_environment",
        "attempt_seq",
        "attempt_id",
    ),
    "transport_outcome_journal": ("attempt_id",),
    "quarantine_opening": (
        "exchange_account_id",
        "deployment_environment",
        "opened_revision",
        "quarantine_id",
    ),
    "quarantine_member": ("quarantine_id", "source_kind", "venue_object_id"),
    "execution_resolution_journal": (
        "exchange_account_id",
        "deployment_environment",
        "resolved_at_ms",
        "id",
    ),
    "accepted_capital_basis": ("exchange_account_id", "deployment_environment", "id"),
    "accepted_capital_basis_symbol": ("basis_id", "symbol"),
    "accepted_capital_basis_cell": ("basis_id", "symbol", "cell_id"),
    "accepted_capital_basis_credit": ("basis_id", "source_kind", "venue_credit_id"),
    "accepted_capital_basis_credit_cell": (
        "basis_id",
        "source_kind",
        "venue_credit_id",
        "cell_id",
    ),
    "accepted_capital_basis_attempt": ("basis_id", "attempt_id"),
    "accepted_capital_basis_quarantine": ("basis_id", "quarantine_id"),
    "capital_authority_epoch": ("epoch_seq",),
}

# The column whose maximum is the table's watermark; None where no watermark applies.
WATERMARK_COLUMN: dict[str, str | None] = dict.fromkeys(CANONICAL_COLUMNS) | {
    "capital_command_clock": "revision",
    "ledger_observation_query": "query_revision",
    "submission_attempt_journal": "attempt_seq",
    "quarantine_opening": "opened_revision",
    "capital_authority_epoch": "epoch_seq",
}

# Capture-point-only tables: the clock and the mirrors are updated in place.
MUTABLE_TABLES = frozenset({"capital_command_clock", "venue_offer_mirror", "venue_credit_mirror"})

# How a table reaches its scope (exchange_account_id, deployment_environment):
# None = the table has both columns; (fk column, parent table, parent column) = join the
# parent; absent from the mapping = global (the epoch), never scope-filtered.
_SCOPE_PARENT: dict[str, tuple[str, str, str] | None] = {
    "capital_command_clock": None,
    "ledger_observation_query": None,
    "ledger_observation": None,
    "ledger_observation_wallet": ("observation_id", "ledger_observation", "id"),
    "ledger_observation_offer": ("observation_id", "ledger_observation", "id"),
    "ledger_observation_credit": ("observation_id", "ledger_observation", "id"),
    "ledger_observation_offer_history": ("observation_id", "ledger_observation", "id"),
    "ledger_observation_credit_history": ("observation_id", "ledger_observation", "id"),
    "ledger_observation_trade": ("observation_id", "ledger_observation", "id"),
    "venue_offer_mirror": None,
    "venue_credit_mirror": None,
    "submission_attempt_journal": None,
    "transport_outcome_journal": ("attempt_id", "submission_attempt_journal", "attempt_id"),
    "quarantine_opening": None,
    "quarantine_member": ("quarantine_id", "quarantine_opening", "quarantine_id"),
    "execution_resolution_journal": None,
    "accepted_capital_basis": None,
    "accepted_capital_basis_symbol": ("basis_id", "accepted_capital_basis", "id"),
    "accepted_capital_basis_cell": ("basis_id", "accepted_capital_basis", "id"),
    "accepted_capital_basis_credit": ("basis_id", "accepted_capital_basis", "id"),
    "accepted_capital_basis_credit_cell": ("basis_id", "accepted_capital_basis", "id"),
    "accepted_capital_basis_attempt": ("basis_id", "accepted_capital_basis", "id"),
    "accepted_capital_basis_quarantine": ("basis_id", "accepted_capital_basis", "id"),
}

_ALL_TABLES = cast("tuple[Table, ...]", (*LEDGER_TABLES, CapitalAuthorityEpochRow.__table__))
_TABLES: dict[str, Table] = {table.name: table for table in _ALL_TABLES}
DIGEST_TABLES: tuple[str, ...] = tuple(_TABLES)


def _kind(table: Table, column: str) -> CellKind:
    kind = table.c[column].type
    if isinstance(kind, Boolean):
        return "bool"
    if isinstance(kind, Integer | BigInteger):
        return "int"
    if isinstance(kind, Numeric):
        return "decimal"
    if isinstance(kind, Text):
        return "text"
    if isinstance(kind, Uuid):
        return "uuid"
    if isinstance(kind, SA_JSON):
        return "json"
    raise TypeError(f"{table.name}.{column}: no canonical encoding for {kind!r}")


_KINDS: dict[str, tuple[CellKind, ...]] = {
    name: tuple(_kind(_TABLES[name], column) for column in columns)
    for name, columns in CANONICAL_COLUMNS.items()
}


@dataclass(frozen=True, slots=True)
class TableDigest:
    table: str
    count: int
    sha256: str  # lower-case hex
    watermark: int | None
    mutable: bool  # capture-point digest only: legitimate later writes change it
    scope: Scope | None  # the scope filter applied; None = whole table (or a global table)


@dataclass(frozen=True, slots=True)
class LedgerDigest:
    scope: Scope | None
    tables: tuple[TableDigest, ...]

    def table(self, name: str) -> TableDigest:
        for digest in self.tables:
            if digest.table == name:
                return digest
        raise KeyError(name)


def _json_text(value: str) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _cell(table: str, column: str, kind: CellKind, value: object) -> bytes:
    def bad(why: str) -> TableDigestRowInvalid:
        return TableDigestRowInvalid(f"{table}.{column}: {why}, got {value!r}")

    if value is None:
        return b"null"
    if kind == "bool":
        if not isinstance(value, bool):
            raise bad("expected bool")
        return b"true" if value else b"false"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad("expected int")
        return str(value).encode("ascii")
    if kind == "decimal":
        if not isinstance(value, Decimal) or not value.is_finite():
            raise bad("expected finite Decimal")
        return _json_text(format(value, "f"))
    if kind == "text":
        if not isinstance(value, str):
            raise bad("expected str")
        return _json_text(value)
    if kind == "uuid":
        if isinstance(value, UUID):
            return _json_text(str(value))
        if isinstance(value, str):
            try:
                return _json_text(str(UUID(value)))
            except ValueError:
                raise bad("expected a uuid") from None
        raise bad("expected UUID")
    try:
        return canonical_payload(cast("dict[str, object]", value))
    except (TypeError, ValueError):
        raise bad("expected canonical JSON") from None


def canonical_row_bytes(table: str, row: Row) -> bytes:
    """One row's canonical bytes (no line terminator): the single canonicalizer of both paths."""
    columns = CANONICAL_COLUMNS[table]
    if set(row) != set(columns):
        missing = sorted(set(columns) - set(row))
        extra = sorted(set(row) - set(columns))
        raise TableDigestRowInvalid(f"{table}: row columns differ, missing={missing} extra={extra}")
    kinds = _KINDS[table]
    cells = (
        _cell(table, column, kind, row[column]) for column, kind in zip(columns, kinds, strict=True)
    )
    return b"[" + b",".join(cells) + b"]"


def _order_key(table: str, row: Row) -> tuple[Any, ...]:
    """Python image of the SQL ORDER BY: uuid bytewise, text by UTF-8 bytes, ints numerically."""
    columns = CANONICAL_COLUMNS[table]
    kinds = dict(zip(columns, _KINDS[table], strict=True))
    key: list[Any] = []
    for column in ORDER_BY[table]:
        value = row[column]
        kind = kinds[column]
        if kind == "uuid":
            key.append(UUID(str(value)).int)
        elif kind == "text":
            key.append(cast("str", value).encode("utf-8"))
        else:
            key.append(value)
    return tuple(key)


class _TableHasher:
    def __init__(self, table: str) -> None:
        self._table = table
        self._count = 0
        self._watermark: int | None = None
        self._hash = sha256()
        header = "\n".join((_DOMAIN, table, ",".join(CANONICAL_COLUMNS[table]))) + "\n"
        self._hash.update(header.encode("utf-8"))

    def add(self, row: Row) -> None:
        self._hash.update(canonical_row_bytes(self._table, row) + b"\n")
        self._count += 1
        column = WATERMARK_COLUMN[self._table]
        if column is not None:
            value = cast("int", row[column])
            if self._watermark is None or value > self._watermark:
                self._watermark = value

    def finish(self, scope: Scope | None) -> TableDigest:
        return TableDigest(
            self._table,
            self._count,
            self._hash.hexdigest(),
            self._watermark,
            self._table in MUTABLE_TABLES,
            scope if self._table in _SCOPE_PARENT else None,
        )


def digest_rows(table: str, rows: Iterable[Row], *, scope: Scope | None = None) -> TableDigest:
    """The digest of ``table`` from in-memory rows, in any order.

    ``rows`` must already be the scope's rows when ``scope`` is given (it only labels the
    result); each row has exactly the canonical columns (no generated ones).
    """
    if table not in CANONICAL_COLUMNS:
        raise KeyError(table)
    ordered = sorted(rows, key=lambda row: _order_key(table, row))
    hasher = _TableHasher(table)
    for row in ordered:
        hasher.add(row)
    return hasher.finish(scope)


async def _require_snapshot_transaction(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text(
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only')"
            )
        )
    ).one()
    if (row[0], row[1]) != ("repeatable read", "on"):
        raise TableDigestRefused(
            f"table digest needs REPEATABLE READ READ ONLY, got {row[0]} read_only={row[1]}"
        )


def _statement(name: str, scope: Scope | None) -> tuple[Select[Any], Scope | None]:
    """The explicit-column, totally ordered SELECT (and the scope it applied)."""
    table = _TABLES[name]
    columns = CANONICAL_COLUMNS[name]
    kinds = dict(zip(columns, _KINDS[name], strict=True))
    statement = select(*(table.c[column] for column in columns)).order_by(
        *(
            table.c[column].collate("C") if kinds[column] == "text" else table.c[column]
            for column in ORDER_BY[name]
        )
    )
    if scope is None or name not in _SCOPE_PARENT:
        return statement, None
    parent_link = _SCOPE_PARENT[name]
    if parent_link is None:
        owner = table
    else:
        foreign_key, parent_name, parent_column = parent_link
        owner = _TABLES[parent_name]
        statement = statement.select_from(
            table.join(owner, table.c[foreign_key] == owner.c[parent_column])
        )
    statement = statement.where(
        owner.c.exchange_account_id == scope.exchange_account_id,
        owner.c.deployment_environment == scope.deployment_environment,
    )
    return statement, scope


async def _digest_table(session: AsyncSession, name: str, scope: Scope | None) -> TableDigest:
    statement, applied = _statement(name, scope)
    hasher = _TableHasher(name)
    stream = await session.stream(statement.execution_options(yield_per=_STREAM_BATCH))
    async for row in stream:
        hasher.add(dict(row._mapping))
    return hasher.finish(applied)


async def digest_table(
    session: AsyncSession, table: str, *, scope: Scope | None = None
) -> TableDigest:
    """Count, ordered digest and watermark of one table, inside the caller's transaction."""
    if table not in _TABLES:
        raise KeyError(table)
    await _require_snapshot_transaction(session)
    return await _digest_table(session, table, scope)


async def digest_ledger(session: AsyncSession, *, scope: Scope | None = None) -> LedgerDigest:
    """Every ledger table plus the epoch table, all from the caller's one snapshot.

    ``scope`` restricts each scoped table to that (account, environment); the epoch table is
    global and always whole. The session must be in a REPEATABLE READ READ ONLY transaction.
    """
    await _require_snapshot_transaction(session)
    return LedgerDigest(
        scope, tuple([await _digest_table(session, name, scope) for name in DIGEST_TABLES])
    )
