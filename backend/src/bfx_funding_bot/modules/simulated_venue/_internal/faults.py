"""Seeded, deterministic fault selection. Off unless the plan has rules."""
from __future__ import annotations

import random
from collections import Counter

from bfx_funding_bot.modules.simulated_venue.contracts import (
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
)


class FaultInjector:
    def __init__(self, plan: FaultPlan) -> None:
        self._plan = plan
        self._seen: Counter[FaultTarget] = Counter()

    @property
    def enabled(self) -> bool:
        return bool(self._plan.rules)

    def _fires(self, target: FaultTarget, n: int) -> list[FaultRule]:
        out = []
        for index, rule in enumerate(self._plan.rules):
            if rule.target is not target:
                continue
            if n in rule.ordinals:
                out.append(rule)
            elif rule.probability > 0.0:
                draw = random.Random(f"{self._plan.seed}:{index}:{target.value}:{n}").random()
                if draw < rule.probability:
                    out.append(rule)
        return out

    def next_fault(self, target: FaultTarget) -> FaultKind | None:
        """Count one request of `target` and return the fault to inject, if any."""
        self._seen[target] += 1
        fired = self._fires(target, self._seen[target])
        return fired[0].kind if fired else None

    def request_count(self, target: FaultTarget) -> int:
        """How many requests of `target` were counted so far (the last one's 1-based ordinal)."""
        return self._seen[target]

    def ticks_after(self, request_number: int) -> list[FaultRule]:
        """TICK_AFTER rules due after the `request_number`-th request received."""
        return self._fires(FaultTarget.ANY_REQUEST, request_number)
