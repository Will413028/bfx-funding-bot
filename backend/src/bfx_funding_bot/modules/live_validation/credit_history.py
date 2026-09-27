"""Venue credit history and funding trades: the per-credit read model (I/O).

Attribution needs, per credit, the rate, period, opening and actual close, and
which of our offers produced it. The venue has all of it: ended credits under
``funding/credits/{symbol}/hist`` (and loans, funds lent but not yet used in a
position, under ``funding/loans/{symbol}/hist``) (MTS_LAST_PAYOUT is the real close) and the
matching funding trades under ``funding/trades/{symbol}/hist`` (OFFER_ID links
to our offer). This sync copies both into the database so the weekly report is
reproducible from stored rows and needs no API credentials.

Same shape as ``InterestLedgerSync``: hourly, fail-open, insert-only keyed by
venue ids.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import FundingCreditRecord, FundingTrade
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    DEFAULT_INTERVAL_S,
    INITIAL_LOOKBACK_MS,
    MS_PER_DAY,
    OVERLAP_MS,
)
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingTradeRow,
)

log = logging.getLogger(__name__)

# An ended credit is listed by its MTS_UPDATE, which can be as old as its
# creation (a credit repaid early kept MTS_UPDATE == MTS_CREATE). A credit that
# ends now was therefore created at most one maximum term (120 days) ago, so
# every sync re-reads that far back; inserts are idempotent.
CREDIT_RESYNC_MS = 121 * MS_PER_DAY
_KINDS: tuple[Literal["credit", "loan"], ...] = ("credit", "loan")


class _CreditRest(Protocol):
    async def get_funding_credit_history(
        self, *, ctx: AccountContext, symbol: str, start_ms: int, end_ms: int,
        kind: Literal["credit", "loan"],
    ) -> list[FundingCreditRecord]: ...

    async def get_funding_trades(
        self, *, ctx: AccountContext, symbol: str, start_ms: int, end_ms: int,
    ) -> list[FundingTrade]: ...


class CreditHistorySync:
    def __init__(
        self,
        *,
        rest: _CreditRest,
        ctx: AccountContext,
        session_factory: async_sessionmaker[AsyncSession],
        exchange_account_id: UUID,
        deployment_environment: str,
        symbols: Sequence[str],
        interval_s: int = DEFAULT_INTERVAL_S,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._rest = rest
        self._ctx = ctx
        self._sf = session_factory
        self._account = exchange_account_id
        self._env = deployment_environment
        self._symbols = tuple(symbols)
        self._interval_s = interval_s
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def tick(self) -> None:
        """Sync every symbol once. Never raises."""
        for symbol in self._symbols:
            try:
                credits, trades = await self._sync(symbol)
                if credits or trades:
                    log.info("credit_history_synced symbol=%s credits=%d trades=%d",
                             symbol, credits, trades)
            except Exception:
                log.exception("credit_history_sync_failed symbol=%s (fail-open)", symbol)

    async def _sync(self, symbol: str) -> tuple[int, int]:
        now = self._clock()
        async with self._sf() as session:
            latest_credit = await session.scalar(
                select(func.max(FundingCreditHistoryRow.mts_update)).where(
                    FundingCreditHistoryRow.exchange_account_id == self._account,
                    FundingCreditHistoryRow.symbol == symbol,
                )
            )
            latest_trade = await session.scalar(
                select(func.max(FundingTradeRow.mts_create)).where(
                    FundingTradeRow.exchange_account_id == self._account,
                    FundingTradeRow.symbol == symbol,
                )
            )
        credit_start = now - (INITIAL_LOOKBACK_MS if latest_credit is None else CREDIT_RESYNC_MS)
        trade_start = (now - INITIAL_LOOKBACK_MS if latest_trade is None
                       else max(0, int(latest_trade) - OVERLAP_MS))
        ended = [
            (kind, c)
            for kind in _KINDS
            for c in await self._rest.get_funding_credit_history(
                ctx=self._ctx, symbol=symbol, start_ms=max(0, credit_start), end_ms=now,
                kind=kind,
            )
        ]
        trades = await self._rest.get_funding_trades(
            ctx=self._ctx, symbol=symbol, start_ms=trade_start, end_ms=now,
        )
        credit_rows = [{
            "exchange_account_id": self._account, "kind": kind, "credit_id": c.credit_id,
            "deployment_environment": self._env, "symbol": c.symbol, "side": c.side,
            "mts_create": c.mts_create, "mts_update": c.mts_update, "amount": c.amount,
            "status": c.status, "rate": c.rate, "period": c.period_days,
            "mts_opening": c.mts_opening, "mts_last_payout": c.mts_last_payout,
        } for kind, c in ended if c.mts_last_payout is not None]
        # Insert-only is sound only for settled rows: a row without its final
        # payout could still change, and is picked up by a later re-read.
        trade_rows = [{
            "exchange_account_id": self._account, "trade_id": t.trade_id,
            "deployment_environment": self._env, "symbol": t.symbol,
            "mts_create": t.mts_create, "offer_id": t.offer_id, "amount": t.amount,
            "rate": t.rate, "period": t.period_days, "maker": t.maker,
        } for t in trades]
        return (
            await self._insert(FundingCreditHistoryRow, credit_rows, ("kind", "credit_id")),
            await self._insert(FundingTradeRow, trade_rows, ("trade_id",)),
        )

    async def _insert(self, table: Any, rows: list[dict[str, Any]], key: tuple[str, ...]) -> int:
        if not rows:
            return 0
        async with self._sf() as session:
            dialect = session.get_bind().dialect.name
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = insert(table).values(rows).on_conflict_do_nothing(
                index_elements=["exchange_account_id", *key],
            )
            result = await session.execute(stmt)
            await session.commit()
        return max(0, int(getattr(result, "rowcount", 0) or 0))

    async def run(self, stop_event: asyncio.Event) -> None:
        """Daemon sub-task loop: tick immediately, then every interval_s."""
        log.info("credit_history_sync_started symbols=%s interval_s=%d",
                 ",".join(self._symbols), self._interval_s)
        while not stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
