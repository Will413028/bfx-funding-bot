"""Legacy cycle adapter; BootRecovery's fetch, matching and writes stay unchanged."""

from dataclasses import dataclass, field
from typing import Protocol

from bfx_funding_bot.modules.execution.boot_recovery import ReconcileResult
from bfx_funding_bot.modules.ledger import CycleResult, Scope


class LegacyRecovery(Protocol):
    async def run(self) -> ReconcileResult: ...


@dataclass(frozen=True, slots=True)
class LegacyCycleResult(CycleResult):
    legacy: ReconcileResult = field(kw_only=True)


class LegacyObservationSink:
    def __init__(self, recovery: LegacyRecovery, scope: Scope) -> None:
        self._recovery = recovery
        self._scope = scope

    async def run(self, scope: Scope) -> LegacyCycleResult:
        if scope != self._scope:
            raise ValueError("legacy observation scope mismatch")
        result = await self._recovery.run()
        return LegacyCycleResult("accepted", legacy=result)
