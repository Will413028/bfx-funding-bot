"""Pre-trade guard chain orchestrator.

Behavior:
- Short-circuit on first BLOCK (subsequent guards not evaluated).
- Per-guard timeout asyncio.wait_for(GUARD_EVAL_TIMEOUT_SECONDS) → fail-closed.
- Guard internal exception → fail-closed + safety_trigger level=critical.
- Hard block (guard returned allowed=False) → safety_trigger level=warn.
- Heartbeat emitted on every evaluate() call (whether passed or blocked).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from bfx_funding_bot.modules.execution.emit import emit_safety_trigger
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    GuardResult,
    GuardRule,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

GUARD_EVAL_TIMEOUT_SECONDS = 2.0


class _DiagnosticsProtocol(Protocol):
    """Forensic diagnostics port (→ PG DiagnosticsSink)."""

    async def emit(self, event: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """One guard's verdict inside a dry run."""
    name: str
    allowed: bool
    reason: str | None = None
    # True when the guard timed out or raised — the real path would fail closed
    # here AND emit safety_trigger level=critical.
    internal_error: bool = False


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """What the chain WOULD do with this decision, right now.

    ``would_submit`` is the real short-circuiting verdict (no guard blocked);
    ``blocked_by`` is where the real path would stop. ``guards`` additionally
    carries the verdicts of guards the real path would never have reached —
    that is the diagnostic value: it distinguishes "one knob away from
    resuming" from "three separate things would still block".
    """
    would_submit: bool
    blocked_by: str | None = None
    guards: list[GuardOutcome] = field(default_factory=list)


class SafetyGuardChain:
    def __init__(
        self,
        *,
        guards: list[GuardRule],
        probe: HealthProbe,
        diagnostics: _DiagnosticsProtocol,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
        account_id: str,
    ) -> None:
        self.guards = guards
        self.probe = probe
        self.diagnostics = diagnostics
        self.phase = phase
        self.strategy = strategy
        self.cell = cell
        self.account_id = account_id

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        try:
            for guard in self.guards:
                result, is_internal_error = await self._evaluate_one(
                    guard, decision, ctx,
                )
                if is_internal_error:
                    await self._emit_internal_error(result, decision)
                    return result
                if not result.allowed:
                    await self._emit_block(result, decision)
                    return result
            return GuardResult(allowed=True, guard_name="<chain>")
        finally:
            self.probe.record_heartbeat("safety_chain")

    async def evaluate_before_sizing(
        self, symbol: str, ctx: AccountContext,
    ) -> GuardResult:
        """Run account/symbol uncertainty checks at the sizing boundary.

        ``DeploymentReconciler`` computes an economic gap before it can build a
        full POST decision.  Calling :meth:`evaluate` only after that math made
        an open UNKNOWN exposure observable too late: the allocator had already
        treated the symbol as deployable.  This deliberately evaluates only
        the uncertainty guard with a zero-size probe decision, so allocation,
        buying-power and calibrated guards remain on their normal per-offer
        path while no economic sizing occurs for an uncertain symbol.
        """
        pre_sizing_guards = [
            guard for guard in self.guards if guard.name == "uncertainty"
        ]
        if not pre_sizing_guards:
            return GuardResult(allowed=True, guard_name="<pre_sizing>")
        decision = DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=uuid4(),
            offer_rate=0.0,
            offer_amount_usdt=0.0,
            offer_duration_days=0,
            symbol=symbol,
        )
        try:
            for guard in pre_sizing_guards:
                result, is_internal_error = await self._evaluate_one(
                    guard, decision, ctx,
                )
                if is_internal_error:
                    await self._emit_internal_error(result, decision)
                    return result
                if not result.allowed:
                    await self._emit_block(result, decision)
                    return result
            return GuardResult(allowed=True, guard_name="<pre_sizing>")
        finally:
            self.probe.record_heartbeat("safety_chain")

    async def evaluate_transport(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        """Write eligibility without new spending (reserved submit or cancel).

        Keep the write-shaped probe: SKIP would also bypass uncertainty checks.
        """
        for guard in self.guards:
            if guard.name == "capital_policy":
                continue
            result, _ = await self._evaluate_one(guard, decision, ctx)
            if not result.allowed:
                return result
        return GuardResult(True, "<transport>")

    async def dry_evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> DryRunReport:
        """Answer "would this decision be submitted right now?" WITHOUT side effects.

        Exists because the daemon otherwise exposes only its INPUTS. On
        2026-07-27 the kill switch was set while the funding wallet held 3.00 —
        the reconciler never sized an offer, the chain was never reached, and
        "no orders appeared" was therefore compatible with both a working halt
        and a broken one. Reading BFX_KILL_SWITCH back proved nothing either:
        that is the input we already knew we wrote. This runs the real guards.

        Differences from :meth:`evaluate`, all deliberate:
        - no short-circuit — every guard is evaluated so one report shows
          everything that would block, not just the first thing;
        - no ``safety_trigger`` emit — a probe must never write to the table
          incident response reads as a record of live blocks;
        - no heartbeat — a probe must never forge evidence of trading activity.

        Guard evaluation itself goes through the same :meth:`_evaluate_one` the
        real path uses (same timeout, same fail-closed semantics), so the probe
        cannot drift from the behaviour it claims to describe. Every guard is
        read-only, which is what makes running past the first block safe.

        The executor is not reachable from here: this method only evaluates
        guards and returns a report.
        """
        outcomes: list[GuardOutcome] = []
        blocked_by: str | None = None
        for guard in self.guards:
            result, is_internal_error = await self._evaluate_one(guard, decision, ctx)
            outcomes.append(GuardOutcome(
                name=guard.name,
                allowed=result.allowed,
                reason=result.reason,
                internal_error=is_internal_error,
            ))
            if not result.allowed and blocked_by is None:
                blocked_by = guard.name
        return DryRunReport(
            would_submit=blocked_by is None,
            blocked_by=blocked_by,
            guards=outcomes,
        )

    async def _evaluate_one(
        self, guard: GuardRule, decision: DecisionPayload, ctx: AccountContext,
    ) -> tuple[GuardResult, bool]:
        """Return (result, is_internal_error).

        is_internal_error=True when the guard itself timed out or raised an
        exception — caller emits safety_trigger level=critical.
        is_internal_error=False covers both allow and clean hard-block paths —
        caller emits warn only on hard block.
        """
        try:
            result = await asyncio.wait_for(
                guard.evaluate(decision, ctx),
                timeout=GUARD_EVAL_TIMEOUT_SECONDS,
            )
            return result, False
        except TimeoutError:
            log.error("guard_timeout name=%s", guard.name)
            return (
                GuardResult(
                    allowed=False, guard_name=guard.name,
                    reason=f"eval_timeout >{GUARD_EVAL_TIMEOUT_SECONDS}s",
                ),
                True,
            )
        except Exception as exc:
            log.exception("guard_internal_error name=%s", guard.name)
            return (
                GuardResult(
                    allowed=False, guard_name=guard.name,
                    reason=f"guard_internal_error: {exc!r}",
                ),
                True,
            )

    async def _emit_block(
        self, result: GuardResult, decision: DecisionPayload,
    ) -> None:
        await emit_safety_trigger(
            diagnostics=self.diagnostics,
            phase=self.phase, strategy=self.strategy, cell=self.cell,
            correlation_id=decision.signal_correlation_id,
            account_id=self.account_id,
            level="warn",
            guard_name=result.guard_name,
            reason=result.reason or "blocked",
            decision_snapshot=decision.model_dump(mode="json"),
        )

    async def _emit_internal_error(
        self, result: GuardResult, decision: DecisionPayload,
    ) -> None:
        await emit_safety_trigger(
            diagnostics=self.diagnostics,
            phase=self.phase, strategy=self.strategy, cell=self.cell,
            correlation_id=decision.signal_correlation_id,
            account_id=self.account_id,
            level="critical",
            guard_name=result.guard_name,
            reason=result.reason or "internal",
            decision_snapshot=decision.model_dump(mode="json"),
        )
