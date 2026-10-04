"""TradingStatusService — the daemon's answer to "will you place an order now?"

Before this existed the daemon exposed only its INPUTS: env vars, yaml files,
and log lines. That is what made 2026-07-27 possible.

- An env scalar set to 0 was used to pause the canary. Every configured
  symbol had an explicit cap elsewhere, so the scalar bound nothing and lending
  continued for hours. The value read as "paused"; nothing reported that the
  knob was inert.
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
2. **Report applied authority.** Amounts and per-cell budgets come from
   the canonical capital authority with applied revision/digest and basis.

Read-only throughout: no emit, no state mutation, and the executor is not
reachable from here.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.kill_switch import KillResult
from bfx_funding_bot.modules.execution.safety.trading_state import (
    CAUSE_OPERATOR,
    TradingState,
    TransitionResult,
)
from bfx_funding_bot.modules.ledger import (
    CapitalAuthority,
    CapitalAvailable,
    CapitalBlocked,
    Scope,
    ScopeLock,
)
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
from bfx_funding_bot.modules.strategy import (
    CellConfig,
    DecisionOutcome,
    DecisionPayload,
    configured_symbols,
)
from bfx_funding_bot.modules.trading import CapitalPolicy, CapitalScope, envelope_payload

MANUAL_KILL_GUARD_NAME = "manual_kill"

# Probe defaults when the caller names no rate/period. The AMOUNT is not
# defaulted here — it comes from the configured cell's reference amount, so the
# probe describes an offer the daemon might really place rather than a number
# invented for the report.
_DEFAULT_PROBE_RATE = 0.0001
_DEFAULT_PROBE_PERIOD_DAYS = 2


class _TradingStateProtocol(Protocol):
    async def current(self) -> TradingState | None: ...
    async def transition(
        self, state: str, *, cause: str, actor: str, reason: str,
        now_ms: int | None = None,
    ) -> TransitionResult: ...
    async def history(self, *, limit: int = 20) -> list[TradingState]: ...


class _KillSwitchProtocol(Protocol):
    async def engage(self, *, cause: str, actor: str, reason: str) -> KillResult: ...


@dataclass(frozen=True, slots=True)
class CapitalStatusReads:
    """The applied capital authority the live status reports, for one scope."""

    authority: CapitalAuthority
    lock: ScopeLock
    scope: Scope
    session_factory: async_sessionmaker[AsyncSession]
    clock: Callable[[], int]


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _policy_status(policy: CapitalPolicy) -> dict[str, Any]:
    """JSON-safe policy; the nested envelope carries Decimals too."""
    status: dict[str, Any] = {key: str(value) if isinstance(value, Decimal) else value
                              for key, value in asdict(policy).items()}
    status["envelope"] = None if policy.envelope is None else envelope_payload(policy.envelope)
    return status


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
        exposure: CapitalStatusReads,
        account_ctx: AccountContext,
        cells: list[CellConfig],
        phase: Phase,
        attempts: SubmitAttemptRecorder,
        trading_state: _TradingStateProtocol | None = None,
        kill_switch: _KillSwitchProtocol | None = None,
        deployment: dict[str, Any] | None = None,
        readiness: TradingReadiness | None = None,
    ) -> None:
        self._chain = chain
        # Where exposure comes from: the applied-capital reads.
        self._exposure = exposure
        self._ctx = account_ctx
        self._cells = cells
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
        # Bind diagnostics to the scope actually supplying capital, not an
        # ambient env value or phase label.
        scope = self._exposure.scope
        return {
            "account_id": str(scope.exchange_account_id),
            "deployment_environment": scope.deployment_environment,
        }

    async def snapshot(self) -> dict[str, Any]:
        symbols = {s: await self._capital_status(self._exposure, s)
                   for s in sorted(set(self._symbols) | {"fUST", "fUSD"})}
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
            "symbols": symbols,
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

    async def _capital_status(self, reads: CapitalStatusReads, symbol: str) -> dict[str, Any]:
        scope = reads.scope
        try:
            async with reads.session_factory() as session:
                if not any(cell.symbol == symbol for cell in self._cells):
                    await reads.lock.lock(session, scope)
                    applied = await reads.authority.read_policy(session, scope, symbol)
                    if isinstance(applied, CapitalBlocked):
                        return {"capital_available": False, "reason": applied.reason}
                    return {"capital_available": False,
                        "reason": "policy_disabled" if not applied.policy.enabled else "no_configured_cells",
                        "policy_revision": applied.revision, "policy_digest": applied.digest,
                        "policy": _policy_status(applied.policy)}
                views: dict[str, CapitalAvailable] = {}
                for cell in self._cells:
                    if cell.symbol != symbol:
                        continue
                    read = await reads.authority.read(
                        CapitalScope(scope.exchange_account_id, scope.deployment_environment,
                                     symbol, cell.cell_id),
                        now_ms=reads.clock(), session=session,
                    )
                    if isinstance(read, CapitalBlocked):
                        return {"capital_available": False, "reason": read.reason}
                    views[cell.cell_id] = read
            first = next(iter(views.values()))
            return {
                "capital_available": True,
                "policy_revision": first.applied.revision,
                "policy_digest": first.applied.digest,
                "basis_token": first.basis_token,
                "policy": _policy_status(first.applied.policy),
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
                    # Exact decimal strings, as every other decision record.
                    "offer_amount_usdt": _decimal_text(decision.offer_amount_usdt),
                    "offer_rate": _decimal_text(decision.offer_rate),
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

        SKIP short-circuits the capital guards to allowed, so a SKIP-based probe
        would cheerfully report "nothing blocks" whatever the policy says. The
        probe has to look like the thing being gated.
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
