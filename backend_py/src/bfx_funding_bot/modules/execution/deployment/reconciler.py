"""DeploymentReconciler — the single writer for venue submits.

Called by PeriodicReconcile after each successful reconcile (ledger holds fresh
venue truth). Reads active standing quotes + global exposure, allocates the gap
toward target (= account cap), applies the full safety chain, and submits. The
signal layer no longer submits (single-writer; spec 2026-05-29).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.rest import FundingTicker
from bfx_funding_bot.modules.execution.deployment.book_clamp import (
    TICK,
    ClampPolicy,
    clamp_rate,
)
from bfx_funding_bot.modules.execution.deployment.reprice import (
    RepricePolicy,
    stale_offers,
)
from bfx_funding_bot.modules.execution.deployment.sizing import (
    allocate_gap,
    effective_min_usdt,
)
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.emit import emit_order_submit
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    CancelPort,
    ExecutorPort,
    GuardResult,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.safety.hard_guards import resolve_for_symbol
from bfx_funding_bot.modules.marketfeed.config import CellConfig, configured_symbols
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)


class _LedgerProtocol(Protocol):
    def current_exposure(self, symbol: str) -> Decimal: ...
    def reserved_exposure(self, symbol: str) -> Decimal: ...
    def available_balance(self, symbol: str) -> Decimal: ...


class _SafetyChainProtocol(Protocol):
    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult: ...


class _EventSinkProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class _TickerSourceProtocol(Protocol):
    async def get_funding_ticker(self, *, symbol: str) -> FundingTicker: ...


class DeploymentReconciler:
    def __init__(
        self,
        *,
        store: StandingQuoteStore,
        tracker: CellDeploymentTracker,
        ledger: _LedgerProtocol,
        safety_chain: _SafetyChainProtocol,
        executor: ExecutorPort,
        account_ctx: AccountContext,
        cells: list[CellConfig],
        venue_floor_usd: Decimal,
        min_offer_buffer_pct: Decimal,
        concentration_pct: Decimal,
        balance_buffer_usdt: Decimal,
        caps: dict[str, Decimal] | None = None,
        default_cap: Decimal = Decimal("0"),
        buffers: dict[str, Decimal] | None = None,
        default_buffer: Decimal = Decimal("0"),
        clock: Callable[[], int],
        event_sink: _EventSinkProtocol,
        phase: Phase,
        canceller: CancelPort | None = None,
        reprice: RepricePolicy | None = None,
        ticker_source: _TickerSourceProtocol | None = None,
        clamp: ClampPolicy | None = None,
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._ledger = ledger
        self._safety = safety_chain
        self._executor = executor
        self._ctx = account_ctx
        self._cells = cells
        self._min_fill = effective_min_usdt(venue_floor_usd, min_offer_buffer_pct)
        self._concentration_pct = concentration_pct
        self._balance_buffer = balance_buffer_usdt
        # Phase 2 per-symbol sizing. caps[symbol] / buffers[symbol] are resolved
        # via resolve_for_symbol with the legacy scalars (allocation_cap_usdt /
        # balance_buffer_usdt) as env-fallback, so the resolved cap/buffer match
        # exactly what AllocationCapGuard / BuyingPowerGuard enforce per offer.
        # Defaulting the maps to {} means single-symbol constructions that omit
        # them resolve to the scalar fallbacks (byte-identical to Phase 1).
        self._caps = caps or {}
        self._default_cap = default_cap
        self._buffers = buffers or {}
        self._default_buffer = default_buffer
        self._clock = clock
        self._event_sink = event_sink
        self._phase = phase
        self._canceller = canceller
        self._reprice = reprice
        self._ticker_source = ticker_source
        self._clamp = clamp
        # cell_id → strategy, for the structured ORDER_SUBMIT event envelope.
        self._cell_strategy: dict[str, StrategyName] = {
            c.cell_id: c.strategy for c in cells
        }
        # cell_id → symbol (offer currency), threaded onto the per-offer decision
        # so the per-symbol guards (allocation cap / buying power) can read it, and
        # used for the per-symbol balance clamp.
        self._cell_symbol: dict[str, str] = {c.cell_id: c.symbol for c in cells}

    async def deploy(
        self, *, venue_offers: tuple[ActiveFundingOffer, ...] = (),
    ) -> None:
        # venue_offers: threaded from PeriodicReconcile's reconcile snapshot
        # (E1 stale-offer reprice). Consumed below by _reprice_sweep when a
        # RepricePolicy is configured (self._reprice is not None); otherwise
        # ignored — byte-identical to pre-E1 behavior.
        now = self._clock()
        cancel_budget = (
            self._reprice.max_cancels_per_tick if self._reprice is not None else 0
        )
        # Phase 2: each configured currency is an INDEPENDENT gap pool. The
        # reconciler is the real-money sizing authority, so the sizing math runs
        # once per symbol against THAT symbol's cap[symbol] / buffer[symbol] /
        # exposure / reserved / available — fUST's gap never borrows fUSD's
        # balance and vice versa. Single-currency cells.yaml → one iteration with
        # cap/buffer resolving to the legacy scalars (byte-identical to Phase 1).
        for symbol in configured_symbols(self._cells):
            # Resolve cap/buffer with the SAME three-tier chain the per-offer
            # guards use (map[symbol] → scalar env-fallback → default), so the
            # reconciler sizes to exactly the cap AllocationCapGuard enforces.
            cap = resolve_for_symbol(
                self._caps, symbol, self._ctx.allocation_cap_usdt, self._default_cap,
            )
            if cap == 0:
                # cap=0 ships the currency dark; the AllocationCapGuard also blocks
                # every POST for it (defense-in-depth). Skip sizing entirely so a
                # dark symbol produces zero offers (no wasted guard round-trips).
                continue
            buffer = resolve_for_symbol(
                self._buffers, symbol, self._balance_buffer, self._default_buffer,
            )
            symbol_cells = [c for c in self._cells
                            if self._cell_symbol[c.cell_id] == symbol]
            # E2 book-aware clamp：每 symbol 每 tick 一次 public ticker（免認證，
            # 走共用 FundingRateLimiter，~2 call/90s ≪ 30/min budget）。抓不到
            # → ticker=None → 本 tick 全 fallback（= 現狀行為）；絕不擋 deploy。
            ticker: FundingTicker | None = None
            if self._clamp is not None and self._ticker_source is not None:
                try:
                    ticker = await self._ticker_source.get_funding_ticker(symbol=symbol)
                except Exception:
                    log.warning("clamp_ticker_fetch_failed symbol=%s", symbol, exc_info=True)
            # clamp enabled 時 sweep ref 對齊 book 競爭價（新掛單就掛在這個價位）：
            # 否則 sustained spike 中 sweep 會把 E2 剛掛的高價單當 stale 自砍
            # （cancel/repost churn）。只會抬高 ref（更少 cancel、更保守）；
            # observe mode 不對齊（零行為差）。
            book_competitive = (
                round(ticker.ask - TICK, 10)
                if ticker is not None
                and self._clamp is not None and self._clamp.enabled
                and ticker.ask > TICK
                else None
            )
            e_total = self._ledger.current_exposure(symbol)
            # Clamp the deployable gap to funds physically present in the funding
            # wallet (available − buffer) so the reconciler never sizes an offer the
            # venue must reject for insufficient balance (cap>balance loop, 2026-05-29).
            headroom = max(Decimal("0"), self._ledger.available_balance(symbol) - buffer)
            # Rescale per-cell intent to the *reserved* total (pending open offers),
            # NOT to current_exposure (reserved + realized). Realized credits are
            # committed to the venue and unattributable to any cell — using e_total
            # here would inflate per-cell intent past cap_per_cell (factor > 1) and
            # silently starve cells via negative allocate_gap headroom. cells= scopes
            # the rescale to THIS symbol's cells only (Phase 2 per-currency
            # independence): rescaling fUST's cells must not touch fUSD's.
            cap_per_cell = self._concentration_pct * cap
            self._tracker.reconcile_to_total(
                self._ledger.reserved_exposure(symbol),
                cells=[c.cell_id for c in symbol_cells],
                cap_per_cell=cap_per_cell,
            )

            active = [c.cell_id for c in symbol_cells
                      if self._store.get_active(c.cell_id, now_ms=now) is not None]

            # E1 reprice sweep：先於 allocation。cancel 的 release 由 WS foc /
            # 下次 reconcile 收斂（single-writer ledger），本 tick 的 gap 不變，
            # 釋放資金在下一個 ~90s tick 重掛 — 永不 same-tick double-commit。
            if self._reprice is not None and venue_offers:
                cancel_budget -= await self._reprice_sweep(
                    symbol=symbol,
                    symbol_cells=symbol_cells,
                    venue_offers=venue_offers,
                    now=now,
                    budget=cancel_budget,
                    book_competitive=book_competitive,
                )

            # Fills are pre-computed from this single pre-loop snapshot; the per-cell
            # concentration cap is enforced inside allocate_gap, not incrementally as
            # we record each submit below. Correct within a tick (sum of fills <= gap,
            # each <= per-cell cap); cross-tick drift is corrected by reconcile_to_total.
            # If a WS fill/claim lands mid-tick making this snapshot stale, the per-offer
            # AllocationCapGuard is evaluated ONCE per offer before submit (not at the
            # moment of submission). A fill landing in the narrow window between guard-eval
            # and submit is NOT re-checked, so a brief over-cap is possible but
            # self-corrects on the next ~90 s reconcile. Bitfinex enforces only account
            # balance, not our internal cap. A stale snapshot can only under-deploy
            # (safe), never materially over-deploy.
            fills = allocate_gap(
                target=cap,
                current_exposure=e_total,
                available_headroom=headroom,
                deployed=self._tracker.snapshot(),
                active_cells=active,
                concentration_pct=self._concentration_pct,
                min_fill=self._min_fill,
            )
            if not fills:
                continue  # this symbol has no gap to fill; other symbols still deploy

            gap = cap - e_total
            allocated = sum(fills.values(), Decimal("0"))
            stranded = gap - allocated
            if stranded >= self._min_fill:
                # Attribute the stranded capital to its true cause. When the
                # funding-wallet headroom (available − buffer) binds below the policy
                # gap, the idle capital is balance-limited (cap > balance), not held
                # back by the concentration cap — mislabelling it as concentration
                # sends a partial-deployment operator down the wrong diagnostic path.
                if headroom < gap:
                    log.info(
                        "deployment_capital_stranded gap=%s allocated=%s stranded=%s "
                        "headroom=%s (balance-limited: available−buffer < policy gap) "
                        "active=%s",
                        gap, allocated, stranded, headroom, active,
                    )
                else:
                    log.info(
                        "deployment_capital_stranded gap=%s allocated=%s stranded=%s "
                        "(concentration cap %s/cell or no further active cell) active=%s",
                        gap, allocated, stranded, self._concentration_pct, active,
                    )
            elif stranded > 0:
                log.info(
                    "deployment_capital_stranded_sub_min gap=%s allocated=%s stranded=%s "
                    "(below venue floor, not submitted) active=%s",
                    gap, allocated, stranded, active,
                )

            for cell_id, amount in fills.items():
                quote = self._store.get_active(cell_id, now_ms=now)
                if quote is None:  # defensive: TTL could lapse between checks
                    continue
                # E2 clamp：guard chain、ORDER_SUBMIT event、venue 全部看到
                # clamp 後的 rate（單一 rate 真相）。observe mode 只 log。
                offer_rate = quote.rate
                if self._clamp is not None and ticker is not None and quote.rate is not None:
                    cd = clamp_rate(
                        quote_rate=quote.rate, amount=float(amount),
                        ticker=ticker, policy=self._clamp,
                    )
                    # E2 fix wave: log every branch (TAKER/UNDERCUT/RAISE/FLOOR/
                    # FALLBACK) evaluated here — FLOOR/FALLBACK are the down-side
                    # protection events the observe rollout exists to measure, and
                    # were previously silent (only rate-changed/TAKER logged).
                    log.info(
                        "clamp_%s cell=%s branch=%s quote_rate=%s clamped=%s "
                        "bid=%s ask=%s bid_period=%s amount=%s",
                        "applied" if self._clamp.enabled else "would_adjust",
                        cell_id, cd.branch, quote.rate, cd.rate,
                        ticker.bid, ticker.ask, ticker.bid_period, amount,
                    )
                    if self._clamp.enabled:
                        offer_rate = cd.rate
                decision = DecisionPayload(
                    decision_outcome=DecisionOutcome.POST,
                    signal_correlation_id=quote.signal_correlation_id,
                    offer_rate=offer_rate,
                    offer_amount_usdt=float(amount),
                    offer_duration_days=quote.period_days,
                    symbol=self._cell_symbol[cell_id],
                )
                guard = await self._safety.evaluate(decision, self._ctx)
                if not guard.allowed:
                    log.info(
                        "deployment_skip cell=%s amount=%s guard=%s reason=%s",
                        cell_id, amount, guard.guard_name, guard.reason,
                    )
                    continue
                try:
                    result = await self._executor.submit(decision, self._ctx)
                except Exception:
                    log.exception("deployment_submit_error cell=%s amount=%s", cell_id, amount)
                    continue
                # The live executor does NOT raise on a venue reject (e.g. 10001
                # "not enough balance"): it returns a SubmittedOrder with status
                # "failed". Only record intent + log success when the offer actually
                # landed — otherwise we'd track phantom capital + emit a false
                # deployment_submitted. Next reconcile re-evaluates the gap.
                if result.status == "failed":
                    log.warning(
                        "deployment_submit_rejected cell=%s amount=%s status=%s",
                        cell_id, amount, result.status,
                    )
                    await self._emit_submit(cell_id, decision, result)
                    continue
                self._tracker.record_deploy(cell_id, amount)
                log.info("deployment_submitted cell=%s amount=%s", cell_id, amount)
                await self._emit_submit(cell_id, decision, result)

    async def _emit_submit(
        self, cell_id: str, decision: DecisionPayload, result: SubmittedOrder,
    ) -> None:
        """Structured ORDER_SUBMIT event for the live deploy path — parity with
        SIGNAL/DECISION + the paper executor, so a structured-event dashboard can
        see live deploys (and venue rejects), not just plain log lines. The
        reconciler is the single live writer (built only when not simulated), so
        is_simulated is always False here."""
        # OrderSubmitPayload requires a failure_reason whenever status != submitted.
        failure_reason = (
            None if result.status == "submitted"
            else (str(result.raw_response) if result.raw_response else "venue_rejected")
        )
        await emit_order_submit(
            event_sink=self._event_sink,
            phase=self._phase,
            strategy=self._cell_strategy[cell_id],
            cell=cell_id,
            decision=decision,
            ctx=self._ctx,
            cid=result.cid,
            offer_id=result.venue_offer_id,
            is_simulated=False,
            status=result.status,
            failure_reason=failure_reason,
        )

    async def _reprice_sweep(
        self,
        *,
        symbol: str,
        symbol_cells: list[CellConfig],
        venue_offers: tuple[ActiveFundingOffer, ...],
        now: int,
        budget: int,
        book_competitive: float | None = None,
    ) -> int:
        """砍掉 rate 已 stale-high 的 resting offers（policy 見 reprice.py）。

        回傳實際發出的 cancel 數（observe mode 恆 0）。任何非 auth 錯誤只
        log 不擋部署（sweep 是 best-effort 最佳化，deploy 才是主線）。
        """
        assert self._reprice is not None
        quotes = [
            q for q in (
                self._store.get_active(c.cell_id, now_ms=now) for c in symbol_cells
            )
            if q is not None and q.rate is not None
        ]
        if not quotes:
            # 無 active POST quote：resting 高價單 = 免費 spike option，留著。
            # 下一個 POST quote 出現時本 sweep 自然會 reprice-down。
            return 0
        ref = max(quotes, key=lambda q: q.rate or 0.0)
        assert ref.rate is not None  # POST quote 的 rate 必非 None
        ref_rate = ref.rate
        if book_competitive is not None:
            # E2：ref 對齊「現在會掛出的價」（clamp 後）。max() 只會抬高 ref
            # （更少 cancel）；book 下移不加速砍單 — reprice-down 的節奏仍由
            # quote 每小時更新決定（E1 語意不變）。
            ref_rate = max(ref_rate, book_competitive)
        candidates = stale_offers(
            offers=[o for o in venue_offers if o.symbol == symbol],
            ref_rate=ref_rate,
            now_ms=now,
            policy=self._reprice,
        )
        issued = 0
        for offer in candidates:
            age_min = (now - offer.mts_created) / 60_000
            if not self._reprice.enabled or self._canceller is None:
                log.info(
                    "reprice_would_cancel voi=%s symbol=%s offer_rate=%s "
                    "ref_rate=%s age_min=%.0f",
                    offer.venue_offer_id, symbol, offer.rate, ref_rate, age_min,
                )
                continue
            if issued >= budget:
                log.info(
                    "reprice_budget_exhausted symbol=%s deferred=%d",
                    symbol, len(candidates) - issued,
                )
                break
            try:
                await self._canceller.cancel(
                    venue_offer_id=offer.venue_offer_id,
                    signal_correlation_id=ref.signal_correlation_id,
                    account_id=self._ctx.account_id,
                    ctx=self._ctx,
                )
            except ExecutorAuthError:
                raise  # auth 壞掉必須讓 daemon fail-safe 退出，不可吞
            except Exception:
                log.exception("reprice_cancel_error voi=%s", offer.venue_offer_id)
                continue
            issued += 1
            log.info(
                "reprice_cancelled voi=%s symbol=%s offer_rate=%s ref_rate=%s "
                "age_min=%.0f",
                offer.venue_offer_id, symbol, offer.rate, ref_rate, age_min,
            )
        return issued
