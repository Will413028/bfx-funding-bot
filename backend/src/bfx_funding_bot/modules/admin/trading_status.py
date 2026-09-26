"""TradingStatusService — the daemon's answer to "will you place an order now?"

Before this existed the daemon exposed only its INPUTS: env vars, yaml files,
and log lines. That is what made 2026-07-27 possible.

- `BFX_ALLOCATION_CAP_USDT=0` was set to pause the canary. Every configured
  symbol has an explicit cap in the safety config, so the env scalar bound
  nothing and lending continued for hours. The value read as "paused"; only the
  resolution SOURCE would have shown the knob was inert.
- The pause was then "verified" by reading the env var back out of the
  container — checking the input we had just written. That check cannot fail,
  so it proved nothing.
- And with available=3.00 the reconciler never sized an offer, so "no new
  orders appeared" was equally consistent with a working halt and a broken one.

Two design rules follow, and both are load-bearing:

1. **Report behaviour, not configuration.** `halted` is obtained by asking the
   real ManualKillGuard, not by re-reading the trading state. If the guard's
   logic changes, this report changes with it; it cannot describe a rule the
   money path does not follow.
2. **Report applied authority.** Live amounts and per-cell budgets come from
   the canonical CapitalRuntime with applied revision/digest and snapshot fence.
   The tier resolver below is retained only for simulation diagnostics.

Read-only throughout: no emit, no state mutation, and the executor is not
reachable from here.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    resolve_for_symbol_with_source,
)
from bfx_funding_bot.modules.execution.safety.kill_switch import KillResult
from bfx_funding_bot.modules.execution.safety.trading_state import (
    CAUSE_OPERATOR,
    TradingState,
    TransitionResult,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig, configured_symbols
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
)

MANUAL_KILL_GUARD_NAME = "manual_kill"

# Probe defaults when the caller names no rate/period. The AMOUNT is not
# defaulted here — it comes from the configured cell's reference amount, so the
# probe describes an offer the daemon might really place rather than a number
# invented for the report.
_DEFAULT_PROBE_RATE = 0.0001
_DEFAULT_PROBE_PERIOD_DAYS = 2


class _LedgerProtocol(Protocol):
    def current_exposure(self, symbol: str) -> Decimal: ...
    def reserved_exposure(self, symbol: str) -> Decimal: ...
    def realized_exposure(self, symbol: str) -> Decimal: ...
    def available_balance(self, symbol: str) -> Decimal: ...


class _TradingStateProtocol(Protocol):
    async def current(self) -> TradingState | None: ...
    async def transition(
        self, state: str, *, cause: str, actor: str, reason: str,
        now_ms: int | None = None,
    ) -> TransitionResult: ...
    async def history(self, *, limit: int = 20) -> list[TradingState]: ...


class _KillSwitchProtocol(Protocol):
    async def engage(self, *, cause: str, actor: str, reason: str) -> KillResult: ...


def _trading_state_dict(state: TradingState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "state": state.state,
        "halted": not state.allows_new_offers,
        "cause": state.cause,
        "reason": state.reason,
        "actor": state.actor,
        "at_ms": state.created_at_ms,
        "id": state.id,
    }


class TradingStatusService:
    def __init__(
        self,
        *,
        chain: SafetyGuardChain,
        ledger: _LedgerProtocol,
        account_ctx: AccountContext,
        cells: list[CellConfig],
        caps: dict[str, Decimal],
        default_cap: Decimal,
        env_fallback_cap: Decimal | None,
        buffers: dict[str, Decimal],
        default_buffer: Decimal,
        env_fallback_buffer: Decimal | None,
        phase: Phase,
        attempts: SubmitAttemptRecorder,
        trading_state: _TradingStateProtocol | None = None,
        kill_switch: _KillSwitchProtocol | None = None,
        deployment: dict[str, Any] | None = None,
        readiness: TradingReadiness | None = None,
        capital_runtime: CapitalRuntime | None = None,
    ) -> None:
        self._chain = chain
        if phase is Phase.LIVE and capital_runtime is None:
            raise ValueError("live status requires applied capital runtime")
        self._capital = capital_runtime
        self._ledger = ledger
        self._ctx = account_ctx
        self._cells = cells
        self._caps = caps
        self._default_cap = default_cap
        self._env_fallback_cap = env_fallback_cap
        self._buffers = buffers
        self._default_buffer = default_buffer
        self._env_fallback_buffer = env_fallback_buffer
        self._phase = phase
        self._attempts = attempts
        self._trading_state = trading_state
        self._kill_switch = kill_switch
        self._deployment = deployment
        self._readiness = readiness
        self._symbols = sorted(configured_symbols(cells))
        # symbol → reference amount, so the probe's default size has a source.
        self._reference_amount: dict[str, float] = {}
        for c in cells:
            self._reference_amount.setdefault(c.symbol, c.reference_amount_usdt)

    # ---------------------------------------------------------------- status

    def _authority_scope(self) -> dict[str, str | None]:
        # Bind diagnostics to the repository actually supplying capital, not an
        # ambient env value or phase label. Legacy simulations have no live realm.
        repository = self._capital.repository if self._capital else None
        return {
            "account_id": str(repository.account_id) if repository else self._ctx.account_id,
            "deployment_environment": repository.environment if repository else None,
        }

    async def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self._phase.value,
            **self._authority_scope(),
            "configured_cells": [{"symbol": cell.symbol, "cell": cell.cell_id,
                "strategy": cell.strategy.value, "period": cell.period_agg} for cell in self._cells],
            "process_started_at": self._attempts.started_at.isoformat(),
            "halt": await self._halt_state(),
            # This build's change class and what the boot gate did with it.
            "deployment": self._deployment,
            "guards": [
                {"name": g.name}
                for g in self._chain.guards
            ],
            "symbols": {s: await self._capital_status(s) if self._capital else self._symbol_status(s)
                        for s in (sorted(set(self._symbols) | {"fUST", "fUSD"}) if self._capital else self._symbols)},
            "env_fallback_cap": None if self._capital else self._fallback_status(
                self._caps, self._env_fallback_cap, self._default_cap, "cap",
            ),
            "env_fallback_buffer": None if self._capital else self._fallback_status(
                self._buffers, self._env_fallback_buffer, self._default_buffer,
                "buffer",
            ),
            "last_submit_attempt": self._attempts.as_dict(),
            "trading_readiness": self._readiness_dict(),
        }

    def _readiness_dict(self) -> dict[str, object] | None:
        if self._readiness is None:
            return None
        snapshot = self._readiness.snapshot()
        return {
            "trading_ready": snapshot.trading_ready,
            "reason": snapshot.reason,
        }

    async def _halt_state(self) -> dict[str, Any]:
        """Ask the installed ManualKillGuard whether it would block right now.

        Deliberately not a re-read of the stop's input: reading the input
        back is what made the first pause look verified while the bot traded
        on. Asking the guard means this field is the guard's actual verdict.

        A missing guard is its own state. "No guard blocked" and "no guard
        exists to block" both produce halted=False, and reporting them
        identically would let a disabled safety control read as a healthy one.
        """
        sources: dict[str, Any] = {}
        history: list[dict[str, Any]] = []
        if self._trading_state is not None:
            sources["persisted"] = _trading_state_dict(await self._trading_state.current())
            history = [
                d for d in (
                    _trading_state_dict(h)
                    for h in await self._trading_state.history(limit=5)
                ) if d is not None
            ]
        else:
            sources["persisted"] = None

        guard = next(
            (g for g in self._chain.guards if g.name == MANUAL_KILL_GUARD_NAME), None,
        )
        if guard is None:
            return {
                "halted": False,
                "reason": None,
                "guard_installed": False,
                "note": (
                    f"{MANUAL_KILL_GUARD_NAME} guard is not installed — the "
                    "persisted trading state has no effect in this process"
                ),
                "sources": sources,
                "history": history,
            }
        result = await guard.evaluate(self._probe_decision(self._symbols[0]), self._ctx)
        return {
            "halted": not result.allowed,
            "reason": result.reason,
            "guard_installed": True,
            "note": None,
            "sources": sources,
            "history": history,
        }

    # ------------------------------------------------------------ halt

    async def halt(self, *, reason: str, actor: str) -> dict[str, Any]:
        """Kill: HALTED (cause operator), then the venue funding cancel-all.

        The state is written first and stays written whatever the venue does;
        ``cancel_all_complete`` says whether every currency's cancel-all was
        acknowledged. Calling again retries the cancel-all.
        """
        if self._kill_switch is None:
            raise ValueError(
                "kill switch is not configured for this daemon; accepting the "
                "request would report success while changing nothing",
            )
        result = await self._kill_switch.engage(cause=CAUSE_OPERATOR, actor=actor, reason=reason)
        return {
            **_trading_state_dict(result.state),  # type: ignore[dict-item]
            "state_changed": result.state_changed,
            "cancel_all_complete": result.complete,
            "cancel_all": [
                {"currency": o.currency, "phase": o.phase, "venue_status": o.venue_status,
                 "detail": o.detail, "attempt_id": str(o.attempt_id) if o.attempt_id else None,
                 "recorded": o.recorded}
                for o in result.cancel_all
            ],
            "scope_error": result.scope_error,
        }

    def _symbol_status(self, symbol: str) -> dict[str, Any]:
        cap = resolve_for_symbol_with_source(
            self._caps, symbol,
            env_fallback=self._env_fallback_cap, default=self._default_cap,
        )
        buffer = resolve_for_symbol_with_source(
            self._buffers, symbol,
            env_fallback=self._env_fallback_buffer, default=self._default_buffer,
        )
        available = self._ledger.available_balance(symbol)
        # Floor explicitly rather than via max(Decimal("0"), …): max returns its
        # FIRST argument when the two compare equal, so available=3.00 with
        # buffer=3 would render "0" while available=3.10/buffer=3 renders "0.10"
        # — the displayed precision would silently depend on which branch won.
        headroom = available - buffer.value
        if headroom < 0:
            headroom = Decimal("0")
        return {
            "cap": {"value": str(cap.value), "source": cap.source},
            "buffer": {"value": str(buffer.value), "source": buffer.source},
            "exposure": {
                "reserved": str(self._ledger.reserved_exposure(symbol)),
                "realized": str(self._ledger.realized_exposure(symbol)),
                "total": str(self._ledger.current_exposure(symbol)),
            },
            "available_balance": str(available),
            # Same clamp the reconciler applies when sizing. Zero here means no
            # offer can be sized at all — which is why an absence of orders is
            # not evidence that a halt is working.
            "deployable_headroom": str(headroom),
        }

    async def _capital_status(self, symbol: str) -> dict[str, Any]:
        assert self._capital is not None
        try:
            async with self._capital.session_factory() as session:
                if not any(cell.symbol == symbol for cell in self._cells):
                    applied = await self._capital.repository.read_applied(session, symbol=symbol)
                    return {"capital_available": False,
                        "reason": "policy_disabled" if not applied.policy.enabled else "no_configured_cells",
                        "policy_revision": applied.revision, "policy_digest": applied.digest,
                        "policy": {key: str(value) if isinstance(value, Decimal) else value
                                   for key, value in asdict(applied.policy).items()}}
                views = {cell.cell_id: await self._capital.read(
                    symbol=symbol, cell_id=cell.cell_id, session=session,
                ) for cell in self._cells if cell.symbol == symbol}
            first = next(iter(views.values()))
            return {
                "capital_available": True,
                "policy_revision": first.applied.revision,
                "policy_digest": first.applied.digest,
                "snapshot_seq": first.snapshot_seq,
                "policy": {key: str(value) if isinstance(value, Decimal) else value
                           for key, value in asdict(first.applied.policy).items()},
                "available_balance": str(first.snapshot.available_amount),
                "unreflected_commitments": str(first.snapshot.unreflected_commitments),
                "total_capital": str(first.snapshot.total_capital),
                "spendable": str(first.budget.spendable),
                "unattributed_credit_exposure": str(first.unattributed_credit_exposure),
                "cells": {cell: {key: str(value) if isinstance(value, Decimal) else value
                                 for key, value in asdict(view.budget).items()}
                          for cell, view in views.items()},
            }
        except Exception as exc:
            return {"capital_available": False, "reason": str(exc)}

    def _fallback_status(
        self, mapping: dict[str, Decimal], env_fallback: Decimal | None,
        default: Decimal, label: str,
    ) -> dict[str, Any]:
        """Does this legacy env scalar bind anything, and if not, why not?

        The 2026-07-27 answer would have been: value 0, binding false, because
        every configured symbol has an explicit entry.
        """
        binding = [
            s for s in self._symbols
            if resolve_for_symbol_with_source(
                mapping, s, env_fallback=env_fallback, default=default,
            ).source == "env_fallback"
        ]
        if env_fallback is None:
            why = f"env {label} fallback is unset; symbols without an entry use the default"
        elif binding:
            why = f"binds {label} for symbols without an explicit entry: {', '.join(binding)}"
        else:
            why = (
                f"every configured symbol has an explicit {label} in the safety "
                "config, so this value binds nothing"
            )
        return {
            "value": None if env_fallback is None else str(env_fallback),
            "binding": bool(binding),
            "binding_symbols": binding,
            "why": why,
        }

    # ------------------------------------------------------------- dry run

    async def dry_run(
        self,
        *,
        symbol: str | None = None,
        amount: float | None = None,
        rate: float | None = None,
        period_days: int | None = None,
    ) -> dict[str, Any]:
        """Run a synthetic POST per symbol through the real guard chain.

        This is the answer to "the halt is set, but can we prove it blocks?"
        when the funding wallet is too empty for the reconciler to ever reach
        the chain on its own. Nothing is submitted: the executor is not a
        dependency of this class.

        EVERY configured symbol is probed unless one is named. An earlier
        version defaulted to a single symbol and, on the first live run
        (2026-07-27), that default picked fUSD — sorted first and dark, zero
        balance — reporting "blocked by buying_power" while the funded symbol
        was held solely by the kill switch. Reading that default would have
        pointed an operator at funds instead of the halt. A probe that answers
        about a symbol nobody trades is worse than no probe.

        ``would_submit_any`` is the headline: could this daemon place any order
        right now. Each symbol additionally carries the decision it evaluated,
        because a verdict without its input is another thing that cannot be
        checked.
        """
        if symbol is not None and symbol not in self._symbols:
            raise ValueError(
                f"symbol {symbol!r} is not configured (configured: {self._symbols}); "
                "a probe on an untraded symbol describes nothing real",
            )
        targets = [symbol] if symbol is not None else self._symbols
        per_symbol: dict[str, Any] = {}
        for sym in targets:
            decision = self._probe_decision(
                sym, amount=amount, rate=rate, period_days=period_days,
            )
            reports = {cell.cell_id: await self._chain.dry_evaluate(
                decision, replace(self._ctx, capital_cell_id=cell.cell_id),
            ) for cell in self._cells if cell.symbol == sym}
            # A full first cell must not hide spendable headroom in another.
            report = next((r for r in reports.values() if r.would_submit), next(iter(reports.values())))
            per_symbol[sym] = {
                "would_submit": report.would_submit,
                "blocked_by": report.blocked_by,
                "cells": {cell: {"would_submit": r.would_submit, "blocked_by": r.blocked_by}
                          for cell, r in reports.items()},
                "guards": [
                    {
                        "name": g.name, "allowed": g.allowed,
                        "reason": g.reason, "internal_error": g.internal_error,
                    }
                    for g in report.guards
                ],
                "decision": {
                    "decision_outcome": decision.decision_outcome.value,
                    "symbol": decision.symbol,
                    "offer_amount_usdt": decision.offer_amount_usdt,
                    "offer_rate": decision.offer_rate,
                    "offer_duration_days": decision.offer_duration_days,
                },
            }
        return {
            **self._authority_scope(),
            "would_submit_any": any(s["would_submit"] for s in per_symbol.values()),
            "symbols": per_symbol,
        }

    def _probe_decision(
        self, symbol: str, *, amount: float | None = None,
        rate: float | None = None, period_days: int | None = None,
    ) -> DecisionPayload:
        """A synthetic POST — never a SKIP.

        SKIP short-circuits AllocationCapGuard and BuyingPowerGuard to allowed,
        so a SKIP-based probe would cheerfully report "nothing blocks" whatever
        the caps say. The probe has to look like the thing being gated.
        """
        return DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=uuid4(),
            offer_rate=rate if rate is not None else _DEFAULT_PROBE_RATE,
            offer_amount_usdt=(
                amount if amount is not None
                else self._reference_amount.get(symbol, 150.0)
            ),
            offer_duration_days=(
                period_days if period_days is not None else _DEFAULT_PROBE_PERIOD_DAYS
            ),
            symbol=symbol,
        )
