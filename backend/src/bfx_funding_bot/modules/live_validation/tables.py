from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

_NOW = func.current_timestamp()


class AttributionWeeklyRow(Base):
    """E3 (b) — per-cell weekly fee-adjusted realized APR（operator 儀表）。

    由 scripts/run_weekly_attribution.py 每週全量重算 + replace（來源是 venue
    credit history / funding trades，見 credit_attribution；本表是可隨時重建的
    read model）。n_fills = 該週開出的 credit 數。webapi 只 SELECT
    （GRANT 是部署 runbook step，不在 migration）。
    """

    __tablename__ = "attribution_weekly"

    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=True
    )
    cell: Mapped[str] = mapped_column(Text, primary_key=True)
    week_start_ms: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    week_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    n_fills: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_interest_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    net_interest_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    capital_days: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    realized_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_close_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_frr_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_frr_util_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW,
    )


class ConfigRegimeRow(Base):
    """One row per daemon boot — execution-policy regime boundaries.

    Flag flips require a restart (config is boot-immutable), so boots ARE the
    regime boundaries. Task: attribute execution-quality metrics (fill latency,
    realized APR) to the flag state that produced them, without waiting for
    weekly windows to accumulate. Telemetry, not SoT — prunable.
    """

    __tablename__ = "config_regime"

    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_account_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=True
    )
    recorded_at_ms: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    clamp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reprice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(Text, nullable=True)


class FundingInterestPaymentRow(Base):
    """Interest actually paid by the venue: ledger category 28, one row per payout.

    The account-level truth for realized income (net of the venue fee; idle
    capital is already reflected, since ``balance`` is the whole funding
    wallet). Written by ``InterestLedgerSync`` in the bot, idempotent on the
    venue ledger id; a read model the venue can always refill.
    """

    __tablename__ = "funding_interest_payments"

    exchange_account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    ledger_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    mts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW,
    )


class FundingCreditHistoryRow(Base):
    """An ended credit or loan from the venue's history, one row per venue id.

    ``kind`` is ``credit`` or ``loan``: lent funds are listed as loans until a
    borrower uses them in a position, and the two id sequences are separate.

    The per-credit truth for attribution: rate, period, opening and actual close
    (``mts_last_payout``) straight from the venue, instead of the CREDIT_CLOSED
    event, whose rate/period/close were read one slot late until 2026-09-27.
    Written by ``CreditHistorySync`` in the bot; insert-only because an ended
    credit does not change; a read model the venue can always refill.
    """

    __tablename__ = "funding_credit_history"

    exchange_account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    credit_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mts_create: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_update: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    period_days: Mapped[int] = mapped_column("period", Integer, nullable=False)
    mts_opening: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mts_last_payout: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW,
    )

    __table_args__ = (
        CheckConstraint("kind IN ('credit', 'loan')", name="ck_funding_credit_history_kind"),
    )


class FundingTradeRow(Base):
    """A funding trade (one of our offers matched), one row per venue trade id.

    ``offer_id`` links the resulting credit back to the offer we submitted and
    so to its execution decision and cell. Written by ``CreditHistorySync``.
    """

    __tablename__ = "funding_trades"

    exchange_account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    mts_create: Mapped[int] = mapped_column(BigInteger, nullable=False)
    offer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    period_days: Mapped[int] = mapped_column("period", Integer, nullable=False)
    maker: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW,
    )

    __table_args__ = (
        Index(
            "ix_funding_trades_scope_symbol_created",
            "exchange_account_id", "deployment_environment", "symbol", "mts_create",
        ),
    )


# Precedence of the legacy link sources, as the weekly read them before S1-8: offer claims,
# then the venue-offer projection, then ORDER_FILL events (resolve_offer_cells keeps the last
# audited decision and the first signal-correlation fallback, so the order is part of the input).
LEGACY_LINK_SOURCES = ("claim", "venue_offer", "fill")


class AttributionLegacyOfferLinkRow(Base):
    """What the frozen legacy authority recorded about one of our venue offers (S1-8).

    The legacy offer -> decision / signal-correlation links the weekly attribution resolves to
    a cell, copied once by migration ``a0b1c2d3e4f5`` from ``offer_claims``,
    ``venue_offer_state`` and the ``ORDER_FILL`` events of ``event_log`` (frozen since the
    authority switch), so attribution no longer reads those tables. Distinct per source;
    offers placed after the switch are in the ledger journal instead. Owner-written only;
    the weekly (``bfx_bot``) reads it.
    """

    __tablename__ = "attribution_legacy_offer_links"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True,
    )
    exchange_account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    venue_offer_id: Mapped[str] = mapped_column(Text, nullable=False)
    execution_decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "source IN ('claim', 'venue_offer', 'fill')",
            name="ck_attribution_legacy_offer_links_source",
        ),
        CheckConstraint(
            "venue_offer_id <> ''", name="ck_attribution_legacy_offer_links_offer",
        ),
        UniqueConstraint(
            "exchange_account_id", "deployment_environment", "source", "venue_offer_id",
            "execution_decision_id", "signal_correlation_id",
            name="uq_attribution_legacy_offer_links",
            postgresql_nulls_not_distinct=True,
        ),
    )


class AttributionLegacyOpenCreditRow(Base):
    """A credit or loan the legacy authority last saw open, with complete terms (S1-8).

    Copied once by migration ``a0b1c2d3e4f5`` from the non-terminal ``venue_credit_state``
    rows that have a rate, period and creation time (the weekly could not use the others).
    Read only for a credit neither ``funding_credit_history`` nor the ledger mirror knows.
    ``credit_id`` keeps the legacy form (``loan:<id>`` for a loan).
    """

    __tablename__ = "attribution_legacy_open_credits"

    exchange_account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    credit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    mts_created: Mapped[int] = mapped_column(BigInteger, nullable=False)
