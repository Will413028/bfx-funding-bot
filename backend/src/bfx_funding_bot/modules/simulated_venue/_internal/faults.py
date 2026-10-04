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

    def _fires(self, target: FaultTarget, n: int, key: int | None = None) -> list[FaultRule]:
        """Ordinal rules fire on the process ordinal `n`; probability rules draw on `key` (the
        request's durable nonce when the caller has one, else `n`)."""
        key = n if key is None else key
        out = []
        for index, rule in enumerate(self._plan.rules):
            if rule.target is not target:
                continue
            if n in rule.ordinals:
                out.append(rule)
            elif rule.probability > 0.0:
                draw = random.Random(f"{self._plan.seed}:{index}:{target.value}:{key}").random()
                if draw < rule.probability:
                    out.append(rule)
        return out

    def next_fault(self, target: FaultTarget, *, nonce: int | None = None) -> FaultKind | None:
        """Count one request of `target` and return the fault to inject, if any.

        A probability rule draws on `(seed, rule, target, nonce)`: the venue's nonce only ever
        grows, also across restarts, so a new process life never replays the draws of an old
        one (the process ordinal restarts at 1). The same nonce always draws the same.
        """
        self._seen[target] += 1
        fired = self._fires(target, self._seen[target], nonce)
        return fired[0].kind if fired else None

    def request_count(self, target: FaultTarget) -> int:
        """How many requests of `target` were counted so far (the last one's 1-based ordinal)."""
        return self._seen[target]

    def ticks_after(self, request_number: int) -> list[FaultRule]:
        """TICK_AFTER rules due after the `request_number`-th request received."""
        return self._fires(FaultTarget.ANY_REQUEST, request_number)
