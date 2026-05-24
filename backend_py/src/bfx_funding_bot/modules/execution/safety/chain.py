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
from typing import Any, Protocol

from bfx_funding_bot.modules.execution.emit import emit_safety_trigger
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    GuardResult,
    GuardRule,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

GUARD_EVAL_TIMEOUT_SECONDS = 2.0


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class SafetyGuardChain:
    def __init__(
        self,
        *,
        guards: list[GuardRule],
        probe: HealthProbe,
        diagnostics: _AxiomProtocol,
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
