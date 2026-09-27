"""Realized interest from the venue ledger: sync (I/O) and summary (pure).

The account-level truth for "what did lending actually earn": Bitfinex pays
interest once a day per currency, net of its fee, and records the funding
wallet balance with each payout. Dividing one by the other gives the realized
net return on the whole wallet, idle capital included, with no inference from
credit events (whose rate/period were parsed one slot late until 2026-09-27)
and no assumption that a credit ran to term.

Per-cell attribution stays in ``weekly_attribution``; it is reconciled against
this total, not the other way round.

Fail-open like ``BookSnapshotWriter``: an observation is never worth crashing
the daemon over.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.live_validation.tables import FundingInterestPaymentRow

log = logging.getLogger(__name__)

MS_PER_DAY = 86_400_000
DEFAULT_INTERVAL_S = 3600
# First sync reaches this far back; later ones re-read a day of overlap so a
# payout recorded late by the venue is still picked up (inserts are idempotent).
INITIAL_LOOKBACK_MS = 365 * MS_PER_DAY
OVERLAP_MS = MS_PER_DAY


class _LedgerRest(Protocol):
    async def get_interest_payments(
        self, *, ctx: AccountContext, currency: str, start_ms: int, end_ms: int,
    ) -> list[InterestPayment]: ...


def funding_currency(symbol: str) -> str:
    """``fUST`` -> ``UST``: the ledger is keyed by currency, not funding symbol."""
    if not symbol.startswith("f") or len(symbol) < 2:
        raise ValueError(f"not a funding symbol: {symbol!r}")
    return symbol[1:]


class InterestLedgerSync:
    def __init__(
        self,
        *,
        rest: _LedgerRest,
        ctx: AccountContext,
        session_factory: async_sessionmaker[AsyncSession],
        exchange_account_id: UUID,
        deployment_environment: str,
        currencies: Sequence[str],
        interval_s: int = DEFAULT_INTERVAL_S,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._rest = rest
        self._ctx = ctx
        self._sf = session_factory
        self._account = exchange_account_id
        self._env = deployment_environment
        self._currencies = tuple(currencies)
        self._interval_s = interval_s
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def tick(self) -> None:
        """Sync every currency once. Never raises."""
        for currency in self._currencies:
            try:
                inserted = await self._sync(currency)
                if inserted:
                    log.info("interest_ledger_synced currency=%s inserted=%d", currency, inserted)
            except Exception:
                log.exception("interest_ledger_sync_failed currency=%s (fail-open)", currency)

    async def _sync(self, currency: str) -> int:
        now = self._clock()
        async with self._sf() as session:
            latest = await session.scalar(
                select(func.max(FundingInterestPaymentRow.mts)).where(
                    FundingInterestPaymentRow.exchange_account_id == self._account,
                    FundingInterestPaymentRow.currency == currency,
                )
            )
        start = now - INITIAL_LOOKBACK_MS if latest is None else max(0, int(latest) - OVERLAP_MS)
        payments = await self._rest.get_interest_payments(
            ctx=self._ctx, currency=currency, start_ms=start, end_ms=now,
        )
        if not payments:
            return 0
        # Category 28 also carries margin interest; only the funding wallet is lending.
        rows = [{
            "exchange_account_id": self._account, "ledger_id": p.ledger_id,
            "deployment_environment": self._env, "currency": p.currency, "mts": p.mts,
            "amount": p.amount, "balance": p.balance, "description": p.description,
        } for p in payments if p.wallet == "funding"]
        if not rows:
            return 0
        async with self._sf() as session:
            dialect = session.get_bind().dialect.name
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = insert(FundingInterestPaymentRow).values(rows).on_conflict_do_nothing(
                index_elements=["exchange_account_id", "ledger_id"],
            )
            result = await session.execute(stmt)
            await session.commit()
        return max(0, int(getattr(result, "rowcount", 0) or 0))

    async def run(self, stop_event: asyncio.Event) -> None:
        """Daemon sub-task loop: tick immediately, then every interval_s."""
        log.info("interest_ledger_sync_started currencies=%s interval_s=%d",
                 ",".join(self._currencies), self._interval_s)
        while not stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue


@dataclass(frozen=True)
class InterestSummary:
    """Realized net interest over a window, on the funding wallet as a whole."""

    currency: str
    start_ms: int
    end_ms: int
    payouts: int
    net_interest: Decimal
    mean_balance: Decimal | None      # wallet balance before each payout, averaged
    net_apr_pct: Decimal | None       # None without a payout or a positive balance


def summarize_interest(
    payments: Sequence[InterestPayment], *, currency: str, start_ms: int, end_ms: int,
) -> InterestSummary:
    """Net APR = paid / mean wallet balance / window days * 365.

    Each payout covers the day before it, so the window is taken as given
    (e.g. a calendar week) rather than inferred from payout times.
    """
    if end_ms <= start_ms:
        raise ValueError("empty interest window")
    inside = [p for p in payments
              if p.currency == currency and start_ms <= p.mts < end_ms]
    net = sum((p.amount for p in inside), Decimal("0"))
    if not inside:
        # No payout is a real zero once the wallet is known to exist: carry the
        # balance left by the latest earlier payout. Without one there is no
        # balance information at all.
        earlier = [p for p in payments if p.currency == currency and p.mts < start_ms]
        if not earlier:
            return InterestSummary(currency, start_ms, end_ms, 0, net, None, None)
        balance = max(earlier, key=lambda p: (p.mts, p.ledger_id)).balance
        return InterestSummary(currency, start_ms, end_ms, 0, net, balance,
                               Decimal("0") if balance > 0 else None)
    mean_balance = sum((p.balance - p.amount for p in inside), Decimal("0")) / len(inside)
    days = Decimal(end_ms - start_ms) / MS_PER_DAY
    apr = net / mean_balance / days * 365 * 100 if mean_balance > 0 else None
    return InterestSummary(currency, start_ms, end_ms, len(inside), net, mean_balance, apr)


def wallet_balance_basis(
    payments: Sequence[InterestPayment], *, currency: str,
) -> Callable[[int, int], Decimal] | None:
    """Funding-wallet balance per window, from the ledger: the capital budget
    that realized interest is measured against (report_interest's basis).

    For [lo, hi): the mean balance before each payout inside it; without a
    payout inside, the balance after the latest earlier payout, else the
    balance before the first later one. None when the ledger has no payout.
    """
    ordered = sorted((p for p in payments if p.currency == currency),
                     key=lambda p: (p.mts, p.ledger_id))
    if not ordered:
        return None

    def basis(lo: int, hi: int) -> Decimal:
        inside = [p.balance - p.amount for p in ordered if lo <= p.mts < hi]
        if inside:
            return sum(inside, Decimal("0")) / len(inside)
        earlier = [p for p in ordered if p.mts < lo]
        if earlier:
            return earlier[-1].balance
        first_later = ordered[0]
        return first_later.balance - first_later.amount

    return basis
