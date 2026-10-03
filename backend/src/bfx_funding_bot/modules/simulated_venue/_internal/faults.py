"""Seeded, deterministic fault selection. Off unless the plan has rules."""
from __future__ import annotations

import random
from collections import Counter

from bfx_funding_bot.modules.simulated_venue.contracts import (
    FaultKind,
    FaultPlan,
    FaultTarget,
)


class FaultInjector:
    def __init__(self, plan: FaultPlan) -> None:
        self._plan = plan
        self._seen: Counter[FaultTarget] = Counter()

    @property
    def enabled(self) -> bool:
        return bool(self._plan.rules)

    def next_fault(self, target: FaultTarget) -> FaultKind | None:
        """Count one request of `target` and return the fault to inject, if any."""
        self._seen[target] += 1
        n = self._seen[target]
        for index, rule in enumerate(self._plan.rules):
            if rule.target is not target:
                continue
            if n in rule.ordinals:
                return rule.kind
            if rule.probability > 0.0:
                draw = random.Random(f"{self._plan.seed}:{index}:{target.value}:{n}").random()
                if draw < rule.probability:
                    return rule.kind
        return None
