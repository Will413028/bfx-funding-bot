"""DeploymentReconciler — the single writer for venue submits.

Called by PeriodicReconcile after each successful reconcile (ledger holds fresh
venue truth). Reads active standing quotes + global exposure, allocates the gap
toward target (= account cap), applies the full safety chain, and submits. The
signal layer no longer submits (single-writer; spec 2026-05-29).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from math import isfinite
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, uuid5

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.funding_rules import (
    FundingRuleProvider,
    submit_amount,
)
from bfx_funding_bot.modules.execution.amount_fingerprint import (
    choose_fingerprinted_amount,
    fingerprint_of,
)
from bfx_funding_bot.modules.execution.audit import AuditContext
from bfx_funding_bot.modules.execution.capital_repository import (
    CapitalBlockedError,
    read_policy_unlocked,
)
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.contracts import (
    BlockedExecution,
    BlockReason,
    ExecutionPolicy,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.ladder import LadderPolicy, spike_rungs
from bfx_funding_bot.modules.execution.deployment.period_pricing import (
    PeriodPricer,
    PriceBranch,
    PriceDecision,
)
from bfx_funding_bot.modules.execution.deployment.rate_optimizer import (
    OptimizationResult,
    OptimizerNoRecommendation,
    RateCandidate,
    RateOptimizer,
)
from bfx_funding_bot.modules.execution.deployment.reprice import (
    RepricePolicy,
    stale_offers,
    stale_offers_with_refs,
)
from bfx_funding_bot.modules.execution.deployment.sizing import (
    allocate_capital,
)
from bfx_funding_bot.modules.execution.deployment.standing_quote import (
    StandingQuote,
    StandingQuoteStore,
)
from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.emit import emit_order_submit
from bfx_funding_bot.modules.execution.managed_cancel import ManagedOfferSweep
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    CancelPort,
    ExecutorPort,
    GuardResult,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.safety.protection import (
    CAPITAL_BLOCK_TRIGGERS,
    ProtectionPort,
)
from bfx_funding_bot.modules.execution.safety.trading_state import HALTED, read_current
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeKind
from bfx_funding_bot.modules.lending.tracking.artifact import (
    FillModelEvidence,
    FillModelUnavailable,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig, configured_symbols
from bfx_funding_bot.modules.marketfeed.funding_book import (
    BookUnavailable,
    FundingBookProvider,
    MarketSnapshot,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

# A book that never qualified and a book that went stale are different faults
# with different operator responses; the event must say which.
_BOOK_BLOCK_REASONS: dict[BookUnavailable | None, BlockReason] = {
    BookUnavailable.NO_BASELINE: BlockReason.BOOK_NOT_INITIALIZED,
    BookUnavailable.SEQUENCE_GAP: BlockReason.BOOK_SEQUENCE_INVALID,
    BookUnavailable.CHECKSUM_MISMATCH: BlockReason.BOOK_CHECKSUM_INVALID,
    BookUnavailable.STALE: BlockReason.BOOK_STALE,
    BookUnavailable.VENUE_MAINTENANCE: BlockReason.BOOK_VENUE_MAINTENANCE,
}


class _LedgerProtocol(Protocol):
    def current_exposure(self, symbol: str) -> Decimal: ...
    def reserved_exposure(self, symbol: str) -> Decimal: ...
    def available_balance(self, symbol: str) -> Decimal: ...
    def is_uncertain(self, symbol: str) -> bool: ...


class _SafetyChainProtocol(Protocol):
    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult: ...


class _EventSinkProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class _AuditContextFactory(Protocol):
    def build(
        self, *, candidate: DecisionPayload, cell_id: str, reconcile_id: str,
    ) -> AuditContext: ...


class FillModelEvidenceProvider(Protocol):
    """Supplies evidence; absence is intentionally distinct from a score of one."""

    def estimate_fill(
        self,
        reference_rate: Decimal,
        offer_rate: Decimal,
        period_agg: str,
        horizon_h: int,
    ) -> FillModelEvidence | FillModelUnavailable | None: ...


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
        capital_runtime: CapitalRuntime,
        cells: list[CellConfig],
        funding_rules: FundingRuleProvider | None,
        clock: Callable[[], int],
        event_sink: _EventSinkProtocol,
        phase: Phase,
        canceller: CancelPort | None = None,
        reprice: RepricePolicy | None = None,
        ladder: LadderPolicy | None = None,
        attempt_recorder: SubmitAttemptRecorder | None = None,
        book_provider: FundingBookProvider,
        execution_gate: ExecutionGate,
        execution_policy: ExecutionPolicy,
        period_pricer: PeriodPricer,
        audit_context_factory: _AuditContextFactory,
        protection: ProtectionPort | None = None,
        fill_model_provider: FillModelEvidenceProvider | None = None,
        rate_optimizer: RateOptimizer | None = None,
        optimizer_fee_rate: Decimal | None = None,
        optimizer_horizon_h: int | None = None,
        managed_sweep: ManagedOfferSweep | None = None,
    ) -> None:
        self._store = store
        # Level-triggered: while the account is HALTED or a currency's policy is
        # disabled, the managed offers there converge to none (lending envelope
        # D3/D4); None on paper/shadow.
        self._managed_sweep = managed_sweep
        self._tracker = tracker
        self._ledger = ledger
        self._safety = safety_chain
        self._executor = executor
        self._ctx = account_ctx
        self._cells = cells
        self._funding_rules = funding_rules
        if capital_runtime is None:
            raise ValueError("deployment requires explicit applied capital runtime")
        self._capital = capital_runtime
        self._clock = clock
        self._event_sink = event_sink
        self._phase = phase
        self._canceller = canceller
        self._protection = protection
        self._reprice = reprice
        self._ladder = ladder
        self._book_provider = book_provider
        if execution_gate.policy is not execution_policy:
            raise ValueError("execution_gate policy must match execution_policy")
        self._execution_gate = execution_gate
        self._execution_policy = execution_policy
        self._period_pricer = period_pricer
        self._audit_context_factory = audit_context_factory
        self._fill_model_provider = fill_model_provider
        self._rate_optimizer = rate_optimizer or RateOptimizer()
        if execution_policy is ExecutionPolicy.OPTIMIZER_LIVE and optimizer_fee_rate is None:
            raise ValueError("optimizer_live requires optimizer_fee_rate")
        if optimizer_horizon_h is not None and optimizer_horizon_h <= 0:
            raise ValueError("optimizer_horizon_h must be positive")
        self._optimizer_fee_rate = (
            optimizer_fee_rate if optimizer_fee_rate is not None else Decimal("0")
        )
        self._optimizer_horizon_h = optimizer_horizon_h
        # Optional (None on paper/shadow and in most tests): mirrors each submit
        # outcome into a slot GET /admin/trading-status can read. Purely
        # observational — never consulted for a decision.
        self._attempts = attempt_recorder
        # cell_id → strategy, for the structured ORDER_SUBMIT event envelope.
        self._cell_strategy: dict[str, StrategyName] = {
            c.cell_id: c.strategy for c in cells
        }
        # cell_id → symbol (offer currency), threaded onto the per-offer decision
        # so the per-symbol guards (allocation cap / buying power) can read it, and
        # used for the per-symbol balance clamp.
        self._cell_symbol: dict[str, str] = {c.cell_id: c.symbol for c in cells}
        self._cell_period_agg: dict[str, str] = {
            c.cell_id: c.period_agg for c in cells
        }

    async def _pull_if_stopped(self, symbol: str) -> bool:
        """Whether ``symbol`` must place nothing; if so, cancel its managed offers.

        The one place where the offers this bot placed are pulled when trading
        stops (D3 level 3, D4): the account is HALTED (an automatic protection
        or the operator; the operator's kill already sent a venue cancel-all),
        or the currency's applied policy is disabled. Level-triggered: every
        tick re-reads the state and cancels what is still open, so a cancel
        refused or failed on one tick is retried on the next. Foreign offers
        and taken loans are never touched. An unreadable state or policy is not
        "stopped": the capital read below fails closed on it instead.
        """
        try:
            repository = self._capital.repository
            async with self._capital.session_factory() as session:
                policy = await read_policy_unlocked(
                    session, account_id=repository.account_id,
                    environment=repository.environment, symbol=symbol)
                state = await read_current(session, account_id=repository.account_id,
                                           environment=repository.environment)
        except Exception:
            return False
        halted = state is not None and state.state == HALTED
        if policy.enabled and not halted:
            return False
        if self._managed_sweep is not None:
            why = "account HALTED" if halted else "disabled by policy"
            await self._managed_sweep.cancel([symbol], reason=f"{symbol} {why}")
        return True

    async def deploy(self, *, venue_offers: tuple[ActiveFundingOffer, ...] = ()) -> None:
        ctx = self._ctx
        # venue_offers: threaded from PeriodicReconcile's reconcile snapshot
        # (E1 stale-offer reprice). Consumed below by _reprice_sweep when a
        # RepricePolicy is configured (self._reprice is not None); otherwise
        # ignored — byte-identical to pre-E1 behavior.
        now = self._clock()
        reconcile_id = f"reconcile:{now}"
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
            if await self._pull_if_stopped(symbol):
                continue
            # Uncertainty is a sizing-boundary invariant, not merely a
            # per-offer safety check.  The chain's explicit pre-sizing hook is
            # optional for compatibility with small test adapters and older
            # paper implementations.  When present it is the durable authority;
            # the process-local ledger remains the fallback for legacy adapters.
            evaluate_before_sizing = getattr(
                self._safety, "evaluate_before_sizing", None,
            )
            authoritative_uncertainty_guard = evaluate_before_sizing is not None
            if evaluate_before_sizing is not None:
                pre_sizing_result = await evaluate_before_sizing(symbol, ctx)
                if not pre_sizing_result.allowed:
                    log.error(
                        "deployment_symbol_blocked_uncertain_pre_sizing "
                        "account=%s symbol=%s guard=%s reason=%s",
                        self._ctx.account_id,
                        symbol,
                        pre_sizing_result.guard_name,
                        pre_sizing_result.reason,
                    )
                    continue
                # PostgreSQL is authoritative on the live money path.  A
                # resolved uncertainty may leave the paper ledger's process-
                # local counter stale because the API writer runs elsewhere.
                # Once the durable pre-sizing guard allows, converge that
                # compatibility cache before continuing this daemon tick.
                uncertain_exposure = getattr(self._ledger, "uncertain_exposure", None)
                clear_uncertainty = getattr(self._ledger, "clear_uncertainty", None)
                if uncertain_exposure is not None and clear_uncertainty is not None:
                    stale_amount = uncertain_exposure(symbol)
                    if stale_amount > 0:
                        clear_uncertainty(symbol, stale_amount)
            # A post-transport UNKNOWN is an account/symbol-wide command gate:
            # even if the residual cap gap is positive, submitting another
            # offer could duplicate the request that may already exist at the
            # venue.  Legacy adapters without the database pre-sizing hook use
            # the local ledger as their fail-closed authority.
            if not authoritative_uncertainty_guard and self._ledger.is_uncertain(symbol):
                log.error(
                    "deployment_symbol_blocked_uncertain account=%s symbol=%s",
                    self._ctx.account_id, symbol,
                )
                continue
            symbol_cells = [c for c in self._cells
                            if self._cell_symbol[c.cell_id] == symbol]
            active = [c.cell_id for c in symbol_cells
                      if self._store.get_active(c.cell_id, now_ms=now) is not None]
            try:
                if self._funding_rules is None:
                    raise ValueError("funding_rule_unavailable")
                amount_evidence = await self._funding_rules.observe(symbol)
                # One account lock and transaction for the whole symbol's plan.
                async with self._capital.session_factory() as session:
                    views = {cell: await self._capital.read(
                        symbol=symbol, cell_id=cell, session=session,
                    ) for cell in active}
                    # D3a: the amount fingerprints this symbol's live commitments
                    # already hold, read in the same session as the budget.
                    held = set(await self._capital.fingerprints_in_use(
                        symbol=symbol, session=session,
                    ))
                min_fill = submit_amount(amount_evidence, symbol=symbol, now_ms=self._clock())
                fills = allocate_capital(views=views, min_fill=min_fill)
            except Exception as exc:
                log.warning("deployment_capital_unavailable symbol=%s reason=%s", symbol, exc)
                trigger = (CAPITAL_BLOCK_TRIGGERS.get(str(exc))
                           if isinstance(exc, CapitalBlockedError) else None)
                if trigger is not None and self._protection is not None:
                    self._protection.trip(trigger, f"planner capital read for {symbol}: {exc}")
                continue

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
                )

            if not fills:
                continue  # this symbol has no gap to fill; other symbols still deploy

            for cell_id, amount in fills.items():
                now = self._clock()
                # Legacy adapters re-check their local gate.  DB-backed chains
                # instead re-evaluate their uncertainty guard below for each
                # offer, which catches an UNKNOWN opened by the prior submit.
                if (
                    not authoritative_uncertainty_guard
                    and self._ledger.is_uncertain(symbol)
                ):
                    log.error(
                        "deployment_symbol_blocked_uncertain_after_submit "
                        "account=%s symbol=%s cell=%s",
                        self._ctx.account_id, symbol, cell_id,
                    )
                    break
                quote = self._store.get_active(cell_id, now_ms=now)
                if quote is None:  # defensive: TTL could lapse between checks
                    continue
                # Finalise the amount before anything durable names it: the
                # audit row, the intent, the attempt payload and the venue
                # request all carry this exact fingerprinted value.
                planned = amount
                fingerprinted = choose_fingerprinted_amount(
                    planned,
                    seed_key=f"{reconcile_id}:{cell_id}:{quote.signal_correlation_id}",
                    in_use=held, minimum=min_fill,
                    maximum=views[cell_id].applied.policy.max_offer_amount,
                )
                if fingerprinted is None:
                    log.warning(
                        "deployment_skip_no_amount_fingerprint cell=%s symbol=%s planned=%s "
                        "held=%d", cell_id, symbol, planned, len(held),
                    )
                    continue
                amount = fingerprinted
                held.add(cast(int, fingerprint_of(amount)))
                decision = DecisionPayload(
                    decision_outcome=DecisionOutcome.POST,
                    signal_correlation_id=quote.signal_correlation_id,
                    offer_rate=quote.rate,
                    offer_amount_usdt=float(amount),
                    offer_duration_days=quote.period_days,
                    symbol=self._cell_symbol[cell_id],
                )
                cell_ctx = replace(ctx, capital_cell_id=cell_id)
                guard = await self._safety.evaluate(decision, cell_ctx)
                now = self._clock()
                snapshot = self._book_provider.snapshot(symbol, now_ms=now)
                decision_id = str(uuid5(
                    NAMESPACE_URL,
                    f"{reconcile_id}:{cell_id}:{decision.signal_correlation_id}:{amount}",
                ))
                if snapshot is None:
                    unavailable = self._book_provider.unavailable_reason(symbol, now_ms=now)
                    price: PriceDecision | BlockedExecution = BlockedExecution(
                        decision_id=decision_id,
                        candidate=decision,
                        reason=_BOOK_BLOCK_REASONS.get(unavailable, BlockReason.BOOK_STALE),
                        failed_dependency="market_snapshot",
                        evidence={
                            "symbol": symbol,
                            "book_state": unavailable.value if unavailable else "unknown",
                        },
                    )
                else:
                    price = self._period_pricer.price(candidate=decision, snapshot=snapshot)
                gate_price = price
                fill_evidence: FillModelEvidence | FillModelUnavailable | None = None
                optimizer_evidence: Mapping[str, object] | None = None
                optimizer_block_reason: BlockReason | None = None
                period_agg = self._cell_period_agg[cell_id]
                expected_horizon_h, expected_model_version, expected_artifact_hash = (
                    self._optimizer_scope()
                )
                if (
                    isinstance(price, PriceDecision)
                    and self._execution_policy in {
                        ExecutionPolicy.OPTIMIZER_SHADOW,
                        ExecutionPolicy.OPTIMIZER_LIVE,
                    }
                ):
                    # Evidence is estimated per candidate price: the exact-period
                    # book price gates the submit, the signal price competes in the
                    # optimizer on its own estimate.
                    fill_evidence = self._fill_evidence_for(
                        candidate=decision,
                        snapshot=snapshot,
                        price=price,
                        offer_rate=price.rate,
                        now_ms=now,
                        period_agg=period_agg,
                    )
                    signal_evidence = self._fill_evidence_for(
                        candidate=decision,
                        snapshot=snapshot,
                        price=price,
                        offer_rate=Decimal(str(decision.offer_rate)),
                        now_ms=now,
                        period_agg=period_agg,
                    )
                    try:
                        optimization = self._optimize(
                            candidate=decision,
                            price=price,
                            signal_evidence=signal_evidence,
                            price_evidence=fill_evidence,
                        )
                    except Exception:
                        log.exception(
                            "optimizer_failed symbol=%s cell=%s", decision.symbol, cell_id,
                        )
                        optimization = OptimizerNoRecommendation(
                            reason="optimizer_error", candidates=(),
                        )
                    optimizer_evidence = _optimizer_evidence(
                        optimization, price, fill_evidence,
                    )
                    if isinstance(optimization, OptimizationResult):
                        if self._execution_policy is ExecutionPolicy.OPTIMIZER_LIVE:
                            gate_price = PriceDecision(
                                rate=optimization.selected.rate,
                                branch=price.branch,
                                evidence=price.evidence,
                            )
                            # The submit gate must see the evidence of the price it
                            # is about to send, not the book price's.
                            if optimization.selected.fill_evidence is not None:
                                fill_evidence = optimization.selected.fill_evidence
                    elif self._execution_policy is ExecutionPolicy.OPTIMIZER_LIVE:
                        # Never reinterpret a failed optimizer as an implicit signal
                        # fallback.  Valid fill evidence remains visible in audit.
                        if not isinstance(fill_evidence, FillModelUnavailable):
                            optimizer_block_reason = BlockReason.OPTIMIZER_UNAVAILABLE
                    else:
                        await self._emit_shadow_optimizer_unavailable(
                            decision=decision,
                            cell_id=cell_id,
                            reconcile_id=reconcile_id,
                            reason=_optimizer_reason(optimization, fill_evidence),
                        )
                outcome = await self._execution_gate.prepare(
                    decision,
                    decision_id=decision_id,
                    reconcile_id=reconcile_id,
                    snapshot=snapshot,
                    price=gate_price,
                    fill_evidence=fill_evidence,
                    safety=guard,
                    audit_context=replace(self._audit_context_factory.build(
                        candidate=decision, cell_id=cell_id, reconcile_id=reconcile_id,
                    ), strategy=self._cell_strategy[cell_id].value),
                    optimizer_evidence={**(optimizer_evidence or {}),
                                        "funding_amount": amount_evidence.payload()},
                    optimizer_block_reason=optimizer_block_reason,
                    expected_period_agg=period_agg,
                    expected_horizon_h=expected_horizon_h,
                    expected_model_version=expected_model_version,
                    expected_artifact_hash=expected_artifact_hash,
                )
                if not isinstance(outcome, ReadyToSubmit):
                    if isinstance(outcome, BlockedExecution):
                        log.info(
                            "deployment_skip cell=%s amount=%s dependency=%s reason=%s",
                            cell_id, amount, outcome.failed_dependency, outcome.reason.value,
                        )
                        if self._attempts is not None:
                            self._attempts.record_blocked(
                                cell=cell_id, symbol=symbol, amount=amount,
                                guard_name=(
                                    guard.guard_name
                                    if not guard.allowed
                                    else outcome.failed_dependency
                                ),
                                reason=(guard.reason if not guard.allowed else outcome.reason.value),
                            )
                    else:
                        log.info(
                            "deployment_no_recommendation cell=%s amount=%s reason=%s",
                            cell_id, amount, outcome.reason.value,
                        )
                    continue
                if self._ladder is not None and isinstance(price, PriceDecision):
                    ask_rate = price.evidence.get("ask_rate")
                    if isinstance(ask_rate, str):
                        rungs = spike_rungs(
                            amount=float(amount), ask=float(ask_rate), policy=self._ladder,
                        )
                        if rungs:
                            log.info(
                                "ladder_would_post cell=%s base_rate=%s rungs=%s ask=%s",
                                cell_id, outcome.decision.offer_rate,
                                [(round(a, 2), r) for a, r in rungs], ask_rate,
                            )
                try:
                    outcome = replace(outcome, capital_view=views[cell_id],
                                      funding_amount_evidence=amount_evidence)
                    result = await self._executor.submit(outcome, cell_ctx)
                except Exception as exc:
                    log.exception("deployment_submit_error cell=%s amount=%s", cell_id, amount)
                    if self._attempts is not None:
                        self._attempts.record_error(
                            cell=cell_id, symbol=symbol, amount=amount, reason=repr(exc),
                        )
                    continue
                # Only an explicit ACK may advance the in-memory deployment
                # tracker.  REJECTED/NOT_SENT are capital-neutral; UNKNOWN is
                # pessimistic and must remain blocked until reconcile evidence
                # resolves it.  In particular, UNKNOWN must never fall through
                # the old string-status success branch.
                if result.outcome_kind is SubmitOutcomeKind.UNKNOWN:
                    log.error(
                        "deployment_submit_unknown cell=%s amount=%s reason=%s",
                        cell_id, amount, getattr(result.outcome, "reason", "unknown"),
                    )
                    if self._attempts is not None:
                        self._attempts.record_error(
                            cell=cell_id, symbol=symbol, amount=amount,
                            reason=getattr(result.outcome, "reason", "submit_outcome_unknown"),
                        )
                    await self._emit_submit(cell_id, outcome, result, reconcile_id)
                    continue
                if result.outcome_kind in {
                    SubmitOutcomeKind.REJECTED,
                    SubmitOutcomeKind.NOT_SENT,
                }:
                    log.warning(
                        "deployment_submit_rejected cell=%s amount=%s outcome=%s",
                        cell_id, amount, result.outcome_kind.value,
                    )
                    if self._attempts is not None:
                        if result.outcome_kind is SubmitOutcomeKind.REJECTED:
                            # Guards passed and the venue supplied explicit
                            # rejection evidence; keep this distinct from a
                            # pre-transport validation failure.
                            self._attempts.record_rejected(
                                cell=cell_id, symbol=symbol, amount=amount,
                                reason=getattr(result.outcome, "reason", "venue_rejected"),
                            )
                        else:
                            self._attempts.record_error(
                                cell=cell_id, symbol=symbol, amount=amount,
                                reason="local_pre_transport",
                            )
                    await self._emit_submit(cell_id, outcome, result, reconcile_id)
                    continue
                self._tracker.record_deploy(cell_id, amount)
                log.info("deployment_submitted cell=%s amount=%s", cell_id, amount)
                if self._attempts is not None:
                    self._attempts.record_submitted(
                        cell=cell_id, symbol=symbol, amount=amount,
                    )
                await self._emit_submit(cell_id, outcome, result, reconcile_id)

    def _fill_evidence_for(
        self,
        *,
        candidate: DecisionPayload,
        snapshot: object,
        price: PriceDecision,
        offer_rate: Decimal,
        now_ms: int,
        period_agg: str,
    ) -> FillModelEvidence | FillModelUnavailable | None:
        if self._fill_model_provider is None:
            return None
        horizon_h, _model_version, _artifact_hash = self._optimizer_scope()
        requested_horizon_h = horizon_h if horizon_h is not None else 1
        signal_rate = Decimal(str(candidate.offer_rate))
        reference_rate = _reference_rate(price, fallback=signal_rate)
        try:
            evidence = self._fill_model_provider.estimate_fill(
                reference_rate=reference_rate,
                offer_rate=offer_rate,
                period_agg=period_agg,
                horizon_h=requested_horizon_h,
            )
            if isinstance(evidence, FillModelEvidence) and horizon_h is None:
                return FillModelUnavailable("scope_mismatch")
            return evidence
        except Exception:
            log.exception("fill_model_provider_failed symbol=%s", candidate.symbol)
            return FillModelUnavailable("missing")

    def _optimizer_scope(self) -> tuple[int | None, str | None, str | None]:
        artifact = getattr(self._fill_model_provider, "artifact", None)
        horizon_h = self._optimizer_horizon_h
        if horizon_h is None:
            artifact_horizon_h = getattr(artifact, "horizon_h", None)
            if isinstance(artifact_horizon_h, int):
                horizon_h = artifact_horizon_h
        model_version = getattr(artifact, "model_version", None)
        if not isinstance(model_version, str):
            model_version = None
        artifact_hash = getattr(artifact, "artifact_hash", None)
        if not isinstance(artifact_hash, str):
            artifact_hash = None
        return horizon_h, model_version, artifact_hash

    def _optimize(
        self,
        *,
        candidate: DecisionPayload,
        price: PriceDecision,
        signal_evidence: FillModelEvidence | FillModelUnavailable | None,
        price_evidence: FillModelEvidence | FillModelUnavailable | None,
    ) -> OptimizationResult | OptimizerNoRecommendation | None:
        if not isinstance(signal_evidence, FillModelEvidence):
            return None
        fill_evidence = signal_evidence
        exact_period_evidence = (
            price_evidence if isinstance(price_evidence, FillModelEvidence) else None
        )
        signal_rate = Decimal(str(candidate.offer_rate))
        book_evidence = {
            "snapshot_id": price.evidence.get("snapshot_id"),
            "branch": price.branch.value,
            "exact_period": dict(price.evidence),
        }
        exact_period_candidate = RateCandidate(
            rate=price.rate,
            source=("taker" if price.branch is PriceBranch.TAKER else "maker"),
            fill_evidence=exact_period_evidence,
            book_evidence=book_evidence,
        ) if price.branch in {PriceBranch.TAKER, PriceBranch.UNDERCUT} else None
        return self._rate_optimizer.select(
            signal_rate,
            maker=(
                exact_period_candidate
                if price.branch is PriceBranch.UNDERCUT
                else None
            ),
            taker=(
                exact_period_candidate
                if price.branch is PriceBranch.TAKER
                else None
            ),
            fill_evidence=fill_evidence,
            fee_rate=self._optimizer_fee_rate,
        )

    async def _emit_shadow_optimizer_unavailable(
        self,
        *,
        decision: DecisionPayload,
        cell_id: str,
        reconcile_id: str,
        reason: str,
    ) -> None:
        emit_execution_event = getattr(self._event_sink, "emit_execution_event", None)
        if not callable(emit_execution_event):
            return
        try:
            event_kwargs = {
                "level": "warn",
                "decision_id": str(decision.signal_correlation_id),
                "reconcile_id": reconcile_id,
                "symbol": decision.symbol,
                "cell": cell_id,
                "policy": self._execution_policy.value,
                "outcome": "no_recommendation",
                "reason_code": reason,
                "evidence": {"optimizer_outcome": "no_recommendation"},
            }
            if reason in {
                "fill_model_missing",
                "fill_model_low_confidence",
                "fill_model_scope_mismatch",
                "fill_model_unversioned",
            }:
                await emit_execution_event("funding.fill_model.unavailable", **event_kwargs)
            await emit_execution_event("funding.optimizer.no_recommendation", **event_kwargs)
        except Exception:
            log.debug("shadow_optimizer_event_emit_failed", exc_info=True)

    async def _emit_submit(
        self,
        cell_id: str,
        ready: ReadyToSubmit,
        result: SubmittedOrder,
        reconcile_id: str,
    ) -> None:
        """Structured ORDER_SUBMIT event for the live deploy path — parity with
        SIGNAL/DECISION + the paper executor, so a structured-event dashboard can
        see live deploys (and venue rejects), not just plain log lines. The
        reconciler is the single live writer (built only when not simulated), so
        is_simulated is always False here."""
        # OrderSubmitPayload requires a failure_reason whenever status != submitted.
        failure_reason = (
            None if result.status == "submitted"
            else getattr(result.outcome, "reason", None)
            or (str(result.raw_response) if result.raw_response else "submit_outcome_unknown")
        )
        await emit_order_submit(
            event_sink=self._event_sink,
            phase=self._phase,
            strategy=self._cell_strategy[cell_id],
            cell=cell_id,
            ready=ready,
            ctx=self._ctx,
            cid=result.cid,
            offer_id=result.venue_offer_id,
            is_simulated=False,
            status=result.status,
            failure_reason=failure_reason,
        )
        if result.status != "submitted":
            return
        emit_execution_event = getattr(self._event_sink, "emit_execution_event", None)
        if not callable(emit_execution_event):
            return
        try:
            await emit_execution_event(
                "funding.execution.submitted",
                level="info",
                decision_id=ready.decision_id,
                reconcile_id=reconcile_id,
                symbol=ready.decision.symbol,
                cell=cell_id,
                policy=ready.policy.value,
                outcome="ready",
                reason_code=None,
                evidence={"snapshot_id": ready.market_snapshot_id},
            )
        except Exception:
            log.debug("execution_submitted_event_failed", exc_info=True)

    def _book_reprice_references(
        self,
        *,
        symbol: str,
        quotes: list[StandingQuote],
        offers: list[ActiveFundingOffer],
        now: int,
    ) -> dict[str, float]:
        snapshot = self._book_provider.snapshot(symbol, now_ms=now)
        if snapshot is None:
            log.info(
                "reprice_reference_unavailable symbol=%s reason=%s",
                symbol, self._book_provider.unavailable_reason(symbol, now_ms=now),
            )
            return {}
        refs: dict[str, float] = {}
        for offer in offers:
            ref_rate = _book_reprice_reference_for(
                pricer=self._period_pricer, snapshot=snapshot, symbol=symbol,
                quotes=quotes, offer=offer,
            )
            if ref_rate is not None:
                refs[offer.venue_offer_id] = ref_rate
        return refs

    async def _reprice_sweep(
        self,
        *,
        symbol: str,
        symbol_cells: list[CellConfig],
        venue_offers: tuple[ActiveFundingOffer, ...],
        now: int,
        budget: int,
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
        symbol_offers = [o for o in venue_offers if o.symbol == symbol]
        ref_by_offer: dict[str, float]
        if self._reprice.reference == "book":
            ref_by_offer = self._book_reprice_references(
                symbol=symbol, quotes=quotes, offers=symbol_offers, now=now,
            )
            if not ref_by_offer:
                return 0
            candidates = stale_offers_with_refs(
                offers=symbol_offers, ref_rate_by_offer=ref_by_offer,
                now_ms=now, policy=self._reprice,
            )
        else:
            ref_by_offer = {o.venue_offer_id: ref.rate for o in symbol_offers}
            candidates = stale_offers(
                offers=symbol_offers, ref_rate=ref.rate, now_ms=now, policy=self._reprice,
            )
        issued = 0
        for offer in candidates:
            ref_rate = ref_by_offer[offer.venue_offer_id]
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


def _book_reprice_reference_for(
    *,
    pricer: PeriodPricer,
    snapshot: MarketSnapshot,
    symbol: str,
    quotes: list[StandingQuote],
    offer: ActiveFundingOffer,
) -> float | None:
    """The rate the bot would post now for this offer's period and remaining amount:
    max over the active quotes of that period, priced by the same PeriodPricer that
    prices submissions. None when nothing prices (no quote for the period, or the
    book blocks every candidate) — and None means "do not cancel"."""
    priced: list[float] = []
    for quote in quotes:
        if quote.period_days != offer.period_days or quote.rate is None:
            continue
        candidate = DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=quote.signal_correlation_id,
            offer_rate=quote.rate,
            offer_amount_usdt=float(offer.amount),
            offer_duration_days=offer.period_days,
            symbol=symbol,
        )
        price = pricer.price(candidate=candidate, snapshot=snapshot)
        if isinstance(price, PriceDecision):
            priced.append(float(price.rate))
    return max(priced) if priced else None


def _optimizer_evidence(
    optimization: OptimizationResult | OptimizerNoRecommendation | None,
    price: PriceDecision,
    fill_evidence: FillModelEvidence | FillModelUnavailable | None,
) -> Mapping[str, object]:
    exact_period_book = _json_evidence(price.evidence)
    if isinstance(optimization, OptimizationResult):
        return {
            "outcome": "selected",
            "selected_source": optimization.selected.source,
            "candidate_scores": {
                source: str(score) for source, score in optimization.scores.items()
            },
            "model_version": optimization.model_version,
            "artifact_hash": optimization.artifact_hash,
            "exact_period_book": exact_period_book,
            "candidates": [
                {
                    "source": candidate.source,
                    "rate": str(candidate.rate),
                    "fill_evidence": _fill_evidence_for_audit(candidate.fill_evidence),
                    "book_evidence": _json_evidence(candidate.book_evidence),
                }
                for candidate in optimization.candidates
            ],
        }
    return {
        "outcome": "no_recommendation",
        "reason": _optimizer_reason(optimization, fill_evidence),
        "exact_period_book": exact_period_book,
    }


def _optimizer_reason(
    optimization: OptimizationResult | OptimizerNoRecommendation | None,
    fill_evidence: FillModelEvidence | FillModelUnavailable | None = None,
) -> str:
    if isinstance(optimization, OptimizerNoRecommendation):
        return optimization.reason
    if isinstance(fill_evidence, FillModelUnavailable):
        return f"fill_model_{fill_evidence.reason}"
    return "fill_model_missing"


def _fill_evidence_for_audit(evidence: FillModelEvidence | None) -> Mapping[str, object]:
    if evidence is None:
        return {}
    return {
        "fill_probability": str(evidence.fill_prob),
        "expected_ttf_ms": evidence.expected_ttf_ms,
        "n_samples": evidence.n_samples,
        "symbol": evidence.symbol,
        "period_agg": evidence.period_agg,
        "horizon_h": evidence.horizon_h,
        "cutoff_ms": evidence.cutoff_ms,
    }


def _json_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("evidence keys must be strings")
            copied[key] = _json_evidence(item)
        return copied
    if isinstance(value, tuple | list):
        return [_json_evidence(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("evidence decimal must be finite")
        return str(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError("evidence float must be finite")
        return value
    raise TypeError(f"unsupported JSON evidence value: {type(value).__name__}")


def _reference_rate(price: PriceDecision, *, fallback: Decimal) -> Decimal:
    for key in ("bid_rate", "ask_rate"):
        value = price.evidence.get(key)
        if isinstance(value, str):
            try:
                rate = Decimal(value)
            except Exception:
                continue
            if rate.is_finite() and rate > 0:
                return rate
    return fallback
