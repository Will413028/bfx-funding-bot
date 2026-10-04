"""Ledger S1-1 tables; all new facts are append-only except the clock and mirrors."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column
from sqlalchemy.types import Uuid

from bfx_funding_bot.core.db import Base

_JSON = JSON().with_variant(JSONB, "postgresql")


_INTENDED_AMOUNT_SQL = "(normalized_payload->>'amount')::numeric"
# The match terms of an attempt (S1-3e4b): what ``match_unknown`` reads of the payload, as
# stored generated columns so the web API reads them without being granted the payload.
# A key that is absent (a seeded partial) is NULL; a present rate/period that cannot be cast
# is refused at write, like the amount.
_MATCH_RATE_SQL = "(normalized_payload->>'rate')::numeric"
_MATCH_PERIOD_SQL = "(normalized_payload->>'period')::integer"
_MATCH_TYPE_SQL = (
    "CASE WHEN jsonb_typeof(normalized_payload->'type') = 'string' "
    "THEN normalized_payload->>'type' END"
)
_MATCH_FLAGS_SQL = (
    "CASE WHEN jsonb_typeof(normalized_payload->'flags') IN ('object', 'number') "
    "THEN normalized_payload->'flags' END"
)
# What the loader reads of ``ledger_observation.evidence``; the evidence itself stays ungranted.
_HISTORY_SYMBOLS_SQL = (
    "CASE WHEN jsonb_typeof(evidence->'history_symbols') = 'array' "
    "THEN evidence->'history_symbols' END"
)
_FIRST_PAGE_COUNTS_SQL = (
    "CASE WHEN jsonb_typeof(evidence->'first_page_counts') = 'object' "
    "THEN evidence->'first_page_counts' END"
)

_SQLITE_GENERATED: dict[str, str] = {
    _INTENDED_AMOUNT_SQL: "CAST(json_extract(normalized_payload, '$.amount') AS NUMERIC)",
    _MATCH_RATE_SQL: "CAST(json_extract(normalized_payload, '$.rate') AS NUMERIC)",
    _MATCH_PERIOD_SQL: "CAST(json_extract(normalized_payload, '$.period') AS INTEGER)",
    _MATCH_TYPE_SQL: (
        "CASE WHEN json_type(normalized_payload, '$.type') = 'text' "
        "THEN json_extract(normalized_payload, '$.type') END"
    ),
    _MATCH_FLAGS_SQL: (
        "CASE WHEN json_type(normalized_payload, '$.flags') IN ('object', 'integer', 'real') "
        "THEN json_extract(normalized_payload, '$.flags') END"
    ),
    _HISTORY_SYMBOLS_SQL: (
        "CASE WHEN json_type(evidence, '$.history_symbols') = 'array' "
        "THEN json_extract(evidence, '$.history_symbols') END"
    ),
    _FIRST_PAGE_COUNTS_SQL: (
        "CASE WHEN json_type(evidence, '$.first_page_counts') = 'object' "
        "THEN json_extract(evidence, '$.first_page_counts') END"
    ),
}


@compiles(Computed, "sqlite")
def _sqlite_generated(element: Computed, compiler: Any, **kw: Any) -> str:
    """sqlite stand-ins for the PostgreSQL generated columns (unit-test schema only)."""
    stand_in = _SQLITE_GENERATED.get(str(element.sqltext))
    if stand_in is None:
        raise NotImplementedError(f"no sqlite stand-in for generated column {element.sqltext}")
    return f"GENERATED ALWAYS AS ({stand_in})" + (" STORED" if element.persisted else " VIRTUAL")


def _account() -> MappedColumn[UUID]:
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )


class CapitalPolicyRevisionRow(Base):
    __tablename__ = "capital_policy_revisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    exchange_account_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), nullable=False,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)

    __table_args__ = (UniqueConstraint(
        "exchange_account_id", "deployment_environment", "symbol", "revision",
        name="uq_capital_policy_revision_scope",
    ),)


class CapitalPolicyHeadRow(Base):
    __tablename__ = "capital_policy_heads"

    exchange_account_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("exchange_accounts.id", ondelete="RESTRICT"), primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT"), nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class CapitalAuthorityEpochRow(Base):
    """Which capital authority the runtime runs under; insert-only, latest row wins.

    Only the owner appends (the S1-7 switch); every build reads the latest row
    once at boot and refuses to run on a value it does not support.
    """

    __tablename__ = "capital_authority_epoch"
    epoch_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    authority: Mapped[str] = mapped_column(Text, nullable=False)
    set_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "authority IN ('legacy', 'ledger')", name="ck_capital_authority_epoch_authority"
        ),
    )


class CapitalCommandClockRow(Base):
    __tablename__ = "capital_command_clock"
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_capital_command_clock_revision"),)


class LedgerObservationQueryRow(Base):
    __tablename__ = "ledger_observation_query"
    query_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    query_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "exchange_account_id",
            "deployment_environment",
            "query_revision",
            name="uq_ledger_observation_query_scope_revision",
        ),
        CheckConstraint(
            "query_revision > 0 AND started_at_ms >= 0 AND start_revision >= 0",
            name="ck_ledger_observation_query_nonnegative",
        ),
        Index(
            "ix_ledger_observation_query_scope_revision",
            "exchange_account_id",
            "deployment_environment",
            text("query_revision DESC"),
        ),
    )


class LedgerObservationRow(Base):
    __tablename__ = "ledger_observation"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    query_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation_query.query_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    query_finished_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confirmation_finished_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accept_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # ``legacy_seed``: the one-time closure seed, owner-written only (never by a runtime role,
    # never evidence of a resolution); every runtime observation is ``venue``.
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'venue'"))
    wallets_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    offers_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    credits_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    loans_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    offer_history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    credit_history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trades_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trades_requested_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    trades_requested_end_ms: Mapped[int | None] = mapped_column(BigInteger)
    offer_history_pages: Mapped[int | None] = mapped_column(Integer)
    credit_history_pages: Mapped[int | None] = mapped_column(Integer)
    history_requested_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    history_requested_end_ms: Mapped[int | None] = mapped_column(BigInteger)
    history_oldest_mts_created: Mapped[int | None] = mapped_column(BigInteger)
    history_newest_mts_created: Mapped[int | None] = mapped_column(BigInteger)
    first_digest: Mapped[str] = mapped_column(Text, nullable=False)
    confirmation_digest: Mapped[str] = mapped_column(Text, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    # Readable by the web API without granting ``evidence``; never written.
    history_symbols: Mapped[list[Any] | None] = mapped_column(
        _JSON, Computed(_HISTORY_SYMBOLS_SQL, persisted=True)
    )
    first_page_counts: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON, Computed(_FIRST_PAGE_COUNTS_SQL, persisted=True)
    )
    __table_args__ = (
        UniqueConstraint("id", "accepted", name="uq_ledger_observation_accepted"),
        CheckConstraint(
            "schema_version >= 1 AND query_finished_at_ms >= 0 AND "
            "confirmation_finished_at_ms >= query_finished_at_ms AND accept_revision >= 0",
            name="ck_ledger_observation_order",
        ),
        CheckConstraint("origin IN ('venue', 'legacy_seed')", name="ck_ledger_observation_origin"),
        CheckConstraint(
            "NOT accepted OR origin <> 'venue' OR (wallets_complete AND "
            "offers_complete AND credits_complete AND loans_complete AND "
            "offer_history_complete AND credit_history_complete AND trades_complete)",
            name="ck_ledger_observation_acceptance",
        ),
        CheckConstraint(
            "origin <> 'legacy_seed' OR (accepted AND NOT wallets_complete AND "
            "NOT offer_history_complete AND NOT credit_history_complete AND "
            "NOT trades_complete AND trades_requested_start_ms IS NULL AND "
            "history_requested_start_ms IS NULL AND history_oldest_mts_created IS NULL AND "
            "offer_history_pages IS NULL AND credit_history_pages IS NULL)",
            name="ck_ledger_observation_seed",
        ),
        CheckConstraint(
            "(trades_requested_start_ms IS NULL) = (trades_requested_end_ms IS NULL) AND "
            "(trades_requested_start_ms IS NULL OR (trades_requested_start_ms >= 0 AND "
            "trades_requested_end_ms >= trades_requested_start_ms))",
            name="ck_ledger_observation_trade_range",
        ),
        CheckConstraint(
            "(history_requested_start_ms IS NULL) = (history_requested_end_ms IS NULL) "
            "AND (history_requested_start_ms IS NULL OR (history_requested_start_ms >= 0 "
            "AND history_requested_end_ms >= history_requested_start_ms)) AND "
            "(history_oldest_mts_created IS NULL) = (history_newest_mts_created IS NULL) "
            "AND (history_oldest_mts_created IS NULL OR (history_oldest_mts_created >= 0 "
            "AND history_newest_mts_created >= history_oldest_mts_created))",
            name="ck_ledger_observation_history_range",
        ),
        CheckConstraint(
            "first_digest = confirmation_digest", name="ck_ledger_observation_matching_digest"
        ),
        CheckConstraint(
            "(offer_history_pages IS NULL OR offer_history_pages >= 0) AND "
            "(credit_history_pages IS NULL OR credit_history_pages >= 0)",
            name="ck_ledger_observation_history_pages",
        ),
        Index(
            "ix_ledger_observation_scope_finished",
            "exchange_account_id",
            "deployment_environment",
            text("query_finished_at_ms DESC"),
            "id",
        ),
    )


class LedgerObservationWalletRow(Base):
    __tablename__ = "ledger_observation_wallet"
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    wallet_type: Mapped[str] = mapped_column(Text, primary_key=True)
    currency: Mapped[str] = mapped_column(Text, primary_key=True)
    available: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # Funding symbol the port assigns to a funding wallet; NULL for other wallets.
    symbol: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("available >= 0 AND balance >= 0", name="ck_ledger_wallet_amount"),
        UniqueConstraint("observation_id", "symbol", name="uq_ledger_observation_wallet_symbol"),
    )


class LedgerObservationOfferRow(Base):
    __tablename__ = "ledger_observation_offer"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_offer_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount_original: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_remaining: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    rate_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer)
    offer_type: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "observation_id", "venue_offer_id", name="uq_ledger_observation_offer_venue"
        ),
        CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) "
            "AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) "
            "AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0)",
            name="ck_ledger_observation_offer_amount",
        ),
    )


class LedgerObservationCreditRow(Base):
    __tablename__ = "ledger_observation_credit"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_credit_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    period_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    mts_created: Mapped[int | None] = mapped_column(BigInteger)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    mts_opening: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "observation_id",
            "venue_credit_id",
            "source_kind",
            name="uq_ledger_observation_credit_venue",
        ),
        CheckConstraint(
            "source_kind IN ('credit','loan')", name="ck_ledger_observation_credit_source"
        ),
        CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR "
            "period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND "
            "(mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR "
            "mts_opening >= 0)",
            name="ck_ledger_observation_credit_amount",
        ),
    )


class LedgerObservationOfferHistoryRow(Base):
    __tablename__ = "ledger_observation_offer_history"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_offer_id: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount_original: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_remaining: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    rate_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer)
    offer_type: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    terminal_kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        Index("ix_ledger_observation_offer_history_observation", "observation_id"),
        CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) "
            "AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) "
            "AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0) AND "
            "occurred_at_ms >= 0",
            name="ck_ledger_offer_history_time",
        ),
    )


class LedgerObservationCreditHistoryRow(Base):
    __tablename__ = "ledger_observation_credit_history"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_credit_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    period_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    mts_created: Mapped[int | None] = mapped_column(BigInteger)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    mts_opening: Mapped[int | None] = mapped_column(BigInteger)
    terminal_kind: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        Index("ix_ledger_observation_credit_history_observation", "observation_id"),
        CheckConstraint("source_kind IN ('credit','loan')", name="ck_ledger_credit_history_source"),
        CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR "
            "period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND "
            "(mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR "
            "mts_opening >= 0) AND occurred_at_ms >= 0",
            name="ck_ledger_credit_history_time",
        ),
    )


class LedgerObservationTradeRow(Base):
    """Funding trades fetched with the first observation (evidence, not in the digest)."""

    __tablename__ = "ledger_observation_trade"
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    trade_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    mts_create: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maker: Mapped[bool | None] = mapped_column(Boolean)
    __table_args__ = (
        CheckConstraint(
            "trade_id >= 0 AND amount > 0 AND rate >= 0 AND period_days > 0 AND mts_create >= 0",
            name="ck_ledger_observation_trade_amount",
        ),
    )


class VenueOfferMirrorRow(Base):
    __tablename__ = "venue_offer_mirror"
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_offer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount_original: Mapped[Decimal | None] = mapped_column(Numeric)
    amount_remaining: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    rate_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer)
    offer_type: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    last_accepted_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    present_in_latest_accepted_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    terminal_evidence_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation_offer_history.id", ondelete="RESTRICT"),
    )
    terminal_kind: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint(
            "amount_remaining >= 0 AND (amount_original IS NULL OR amount_original >= 0) "
            "AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR period_days > 0) "
            "AND mts_created >= 0 AND (mts_updated IS NULL OR mts_updated >= 0)",
            name="ck_venue_offer_mirror_amount",
        ),
        CheckConstraint(
            "(terminal_evidence_id IS NULL) = (terminal_kind IS NULL) AND "
            "(terminal_evidence_id IS NULL OR NOT present_in_latest_accepted_snapshot)",
            name="ck_venue_offer_mirror_terminal",
        ),
        Index(
            "ix_venue_offer_mirror_live",
            "exchange_account_id",
            "deployment_environment",
            "symbol",
            postgresql_where=text("present_in_latest_accepted_snapshot"),
        ),
    )


class VenueCreditMirrorRow(Base):
    __tablename__ = "venue_credit_mirror"
    exchange_account_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_credit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal | None] = mapped_column(Numeric)
    period_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    flags: Mapped[dict[str, Any] | int | None] = mapped_column(_JSON)
    mts_created: Mapped[int | None] = mapped_column(BigInteger)
    mts_updated: Mapped[int | None] = mapped_column(BigInteger)
    mts_opening: Mapped[int | None] = mapped_column(BigInteger)
    last_accepted_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    present_in_latest_accepted_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    terminal_evidence_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation_credit_history.id", ondelete="RESTRICT"),
    )
    terminal_kind: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("source_kind IN ('credit','loan')", name="ck_venue_credit_mirror_source"),
        CheckConstraint(
            "amount >= 0 AND (rate IS NULL OR rate >= 0) AND (period_days IS NULL OR "
            "period_days > 0) AND (mts_created IS NULL OR mts_created >= 0) AND "
            "(mts_updated IS NULL OR mts_updated >= 0) AND (mts_opening IS NULL OR "
            "mts_opening >= 0)",
            name="ck_venue_credit_mirror_amount",
        ),
        CheckConstraint(
            "(terminal_evidence_id IS NULL) = (terminal_kind IS NULL) AND "
            "(terminal_evidence_id IS NULL OR NOT present_in_latest_accepted_snapshot)",
            name="ck_venue_credit_mirror_terminal",
        ),
        Index(
            "ix_venue_credit_mirror_live",
            "exchange_account_id",
            "deployment_environment",
            "symbol",
            postgresql_where=text("present_in_latest_accepted_snapshot"),
        ),
    )


class SubmissionAttemptJournalRow(Base):
    __tablename__ = "submission_attempt_journal"
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    execution_decision_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("execution_decisions.decision_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    cell_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "accepted_capital_basis.id", ondelete="RESTRICT", name="fk_submission_attempt_basis"
        ),
        nullable=False,
    )
    # NULL only for a seeded attempt (the legacy authorizing revision was never stored).
    policy_revision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    authorization_evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    seed_provenance: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Readable by the web API without granting ``normalized_payload``; never written.
    intended_amount: Mapped[Decimal] = mapped_column(
        Numeric, Computed(_INTENDED_AMOUNT_SQL, persisted=True), nullable=False
    )
    # The match terms (S1-3e4b), likewise readable without the payload; NULL when the payload
    # lacks the key (a seeded partial), which the matcher reads as "not a subject".
    match_rate: Mapped[Decimal | None] = mapped_column(
        Numeric, Computed(_MATCH_RATE_SQL, persisted=True)
    )
    match_period_days: Mapped[int | None] = mapped_column(
        Integer, Computed(_MATCH_PERIOD_SQL, persisted=True)
    )
    match_offer_type: Mapped[str | None] = mapped_column(
        Text, Computed(_MATCH_TYPE_SQL, persisted=True)
    )
    match_flags: Mapped[dict[str, Any] | int | None] = mapped_column(
        _JSON, Computed(_MATCH_FLAGS_SQL, persisted=True)
    )
    __table_args__ = (
        UniqueConstraint(
            "exchange_account_id",
            "deployment_environment",
            "attempt_seq",
            name="uq_submission_attempt_scope_seq",
        ),
        CheckConstraint(
            "attempt_seq >= 0 AND started_at_ms >= 0",
            name="ck_submission_attempt_nonnegative",
        ),
        CheckConstraint(
            "policy_revision_id IS NOT NULL OR seed_provenance IS NOT NULL",
            name="ck_submission_attempt_policy_or_seed",
        ),
        CheckConstraint(
            "intended_amount >= 0 AND intended_amount < 'Infinity'::numeric",
            name="ck_submission_attempt_intended_amount",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "match_rate IS NULL OR match_rate < 'Infinity'::numeric",
            name="ck_submission_attempt_match_rate",
        ).ddl_if(dialect="postgresql"),
    )


class TransportOutcomeJournalRow(Base):
    __tablename__ = "transport_outcome_journal"
    attempt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "submission_attempt_journal.attempt_id",
            ondelete="RESTRICT",
            name="fk_basis_attempt_attempt",
        ),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    completed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ack','rejected','not_sent','unknown')", name="ck_transport_outcome_kind"
        ),
        CheckConstraint(
            "(kind = 'ack') = (venue_offer_id IS NOT NULL)", name="ck_transport_outcome_ack"
        ),
        CheckConstraint("completed_at_ms >= 0", name="ck_transport_outcome_time"),
        Index(
            "ix_transport_outcome_venue_offer",
            "venue_offer_id",
            postgresql_where=text("venue_offer_id IS NOT NULL"),
        ),
    )


class QuarantineOpeningRow(Base):
    __tablename__ = "quarantine_opening"
    quarantine_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    intended_amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    opened_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The command-clock revision the opening bumped to: quarantines opened after a
    # basis are exactly those with ``opened_revision > basis.accept_revision``.
    opened_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    legacy_reconcile_event_seq: Mapped[int | None] = mapped_column(BigInteger)
    source_attempt_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "submission_attempt_journal.attempt_id", ondelete="RESTRICT",
            name="fk_quarantine_opening_source_attempt",
        ),
        nullable=True,
    )
    __table_args__ = (
        CheckConstraint(
            "intended_amount >= 0 AND opened_at_ms >= 0 AND (legacy_reconcile_event_seq "
            "IS NULL OR legacy_reconcile_event_seq >= 0)",
            name="ck_quarantine_opening_nonnegative",
        ),
        CheckConstraint("opened_revision > 0", name="ck_quarantine_opening_revision"),
        Index(
            "uq_quarantine_opening_source_attempt", "source_attempt_id", unique=True,
            postgresql_where=text("source_attempt_id IS NOT NULL"),
        ),
        Index(
            "ix_quarantine_opening_scope_revision",
            "exchange_account_id",
            "deployment_environment",
            "opened_revision",
        ),
    )


class QuarantineMemberRow(Base):
    __tablename__ = "quarantine_member"
    quarantine_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quarantine_opening.quarantine_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_object_id: Mapped[str] = mapped_column(Text, primary_key=True)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount_at_join: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('offer','credit','loan')", name="ck_quarantine_member_kind"
        ),
        CheckConstraint("amount_at_join >= 0", name="ck_quarantine_member_amount"),
    )


class ExecutionResolutionJournalRow(Base):
    __tablename__ = "execution_resolution_journal"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    attempt_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submission_attempt_journal.attempt_id", ondelete="RESTRICT"),
    )
    quarantine_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quarantine_opening.quarantine_id", ondelete="RESTRICT")
    )
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str | None] = mapped_column(Text)
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ledger_observation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    operator_request_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("uncertainty_resolution_requests.request_id", ondelete="RESTRICT"),
    )
    resolved_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_count: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "(attempt_id IS NULL) <> (quarantine_id IS NULL)",
            name="ck_execution_resolution_subject",
        ),
        CheckConstraint(
            "action IN ('bound_to_venue','not_accepted','manual')",
            name="ck_execution_resolution_action",
        ),
        CheckConstraint(
            "(action = 'bound_to_venue') = (venue_offer_id IS NOT NULL)",
            name="ck_execution_resolution_bound",
        ),
        CheckConstraint(
            "action <> 'manual' OR quarantine_id IS NOT NULL",
            name="ck_execution_resolution_manual",
        ),
        CheckConstraint(
            "resolved_at_ms >= 0 AND (candidate_count IS NULL OR candidate_count >= 0)",
            name="ck_execution_resolution_time",
        ),
        Index(
            "uq_execution_resolution_attempt",
            "attempt_id",
            unique=True,
            postgresql_where=text("attempt_id IS NOT NULL"),
        ),
        Index(
            "uq_execution_resolution_quarantine",
            "quarantine_id",
            unique=True,
            postgresql_where=text("quarantine_id IS NOT NULL"),
        ),
        Index(
            "uq_execution_resolution_operator_request",
            "operator_request_id",
            unique=True,
            postgresql_where=text("operator_request_id IS NOT NULL"),
        ),
        Index(
            "ix_execution_resolution_scope_resolved",
            "exchange_account_id",
            "deployment_environment",
            text("resolved_at_ms DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_execution_resolution_venue_offer",
            "venue_offer_id",
            postgresql_where=text("venue_offer_id IS NOT NULL"),
        ),
    )


class AcceptedCapitalBasisRow(Base):
    __tablename__ = "accepted_capital_basis"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    exchange_account_id: Mapped[UUID] = _account()
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    observation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, unique=True)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    accept_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_seq_high_water: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope_block: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["observation_id", "accepted"],
            ["ledger_observation.id", "ledger_observation.accepted"],
            ondelete="RESTRICT",
            name="fk_accepted_basis_observation",
        ),
        CheckConstraint(
            "accepted AND accept_revision >= 0 AND schema_version >= 1 AND accepted_at_ms >= 0",
            name="ck_accepted_basis_acceptance",
        ),
        CheckConstraint("attempt_seq_high_water >= 0", name="ck_accepted_basis_high_water"),
        Index(
            "ix_accepted_capital_basis_scope_accepted",
            "exchange_account_id",
            "deployment_environment",
            text("accepted_at_ms DESC"),
        ),
    )


class AcceptedCapitalBasisSymbolRow(Base):
    __tablename__ = "accepted_capital_basis_symbol"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    available: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    offered: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    credits: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    unattributed_credits: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    foreign_offers: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    block: Mapped[dict[str, Any] | None] = mapped_column(_JSON)
    # Conservation verdict against the scope's previous accepted basis
    # (``ledger/conservation.py``); stored with the basis it was computed on. ``lent_unexplained``
    # is signed: new lending less what offers filled.
    conservation: Mapped[str] = mapped_column(Text, nullable=False)
    lent_unexplained: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    foreign_executed: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    fill_conflicts: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "available >= 0 AND offered >= 0 AND credits >= 0 AND unattributed_credits >= "
            "0 AND foreign_offers >= 0 AND unattributed_credits <= credits",
            name="ck_accepted_basis_symbol_amount",
        ),
        CheckConstraint(
            "foreign_executed >= 0 AND fill_conflicts >= 0 AND CASE conservation "
            "WHEN 'baseline' THEN lent_unexplained = 0 AND foreign_executed = 0 "
            "AND fill_conflicts = 0 "
            "WHEN 'conserved' THEN fill_conflicts = 0 "
            "WHEN 'foreign_lending' THEN fill_conflicts = 0 AND foreign_executed > 0 "
            "WHEN 'unexplained_lending' THEN lent_unexplained <> 0 OR fill_conflicts > 0 "
            "ELSE false END",
            name="ck_accepted_basis_symbol_conservation",
        ),
    )


class AcceptedCapitalBasisCellRow(Base):
    __tablename__ = "accepted_capital_basis_cell"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    cell_id: Mapped[str] = mapped_column(Text, primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_accepted_basis_cell_amount"),)


class AcceptedCapitalBasisCreditRow(Base):
    __tablename__ = "accepted_capital_basis_credit"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_credit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer)
    mts_opening: Mapped[int | None] = mapped_column(BigInteger)
    attribution_basis: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("source_kind IN ('credit','loan')", name="ck_accepted_basis_credit_kind"),
        CheckConstraint("amount >= 0", name="ck_accepted_basis_credit_amount"),
        CheckConstraint(
            "attribution_basis IN ('trade','carry','recent_fill','unattributed')",
            name="ck_accepted_basis_credit_attribution",
        ),
        Index(
            "ix_accepted_basis_credit_cell_lookup",
            "basis_id",
            "symbol",
            "period_days",
            "mts_opening",
        ),
    )


class AcceptedCapitalBasisCreditCellRow(Base):
    __tablename__ = "accepted_capital_basis_credit_cell"
    basis_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_credit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    cell_id: Mapped[str] = mapped_column(Text, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["basis_id", "source_kind", "venue_credit_id"],
            [
                "accepted_capital_basis_credit.basis_id",
                "accepted_capital_basis_credit.source_kind",
                "accepted_capital_basis_credit.venue_credit_id",
            ],
            ondelete="RESTRICT",
            name="fk_accepted_basis_credit_cell_credit",
        ),
    )


class AcceptedCapitalBasisAttemptRow(Base):
    __tablename__ = "accepted_capital_basis_attempt"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("submission_attempt_journal.attempt_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "classification IN ('reflected','settled','unresolved','quarantined')",
            name="ck_accepted_basis_attempt_classification",
        ),
    )


class AcceptedCapitalBasisQuarantineRow(Base):
    __tablename__ = "accepted_capital_basis_quarantine"
    basis_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("accepted_capital_basis.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    quarantine_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quarantine_opening.quarantine_id", ondelete="RESTRICT"),
        primary_key=True,
    )


LEDGER_TABLES = (
    CapitalCommandClockRow.__table__,
    LedgerObservationQueryRow.__table__,
    LedgerObservationRow.__table__,
    LedgerObservationWalletRow.__table__,
    LedgerObservationOfferRow.__table__,
    LedgerObservationCreditRow.__table__,
    LedgerObservationOfferHistoryRow.__table__,
    LedgerObservationCreditHistoryRow.__table__,
    LedgerObservationTradeRow.__table__,
    VenueOfferMirrorRow.__table__,
    VenueCreditMirrorRow.__table__,
    SubmissionAttemptJournalRow.__table__,
    TransportOutcomeJournalRow.__table__,
    QuarantineOpeningRow.__table__,
    QuarantineMemberRow.__table__,
    ExecutionResolutionJournalRow.__table__,
    AcceptedCapitalBasisRow.__table__,
    AcceptedCapitalBasisSymbolRow.__table__,
    AcceptedCapitalBasisCellRow.__table__,
    AcceptedCapitalBasisCreditRow.__table__,
    AcceptedCapitalBasisCreditCellRow.__table__,
    AcceptedCapitalBasisAttemptRow.__table__,
    AcceptedCapitalBasisQuarantineRow.__table__,
)
