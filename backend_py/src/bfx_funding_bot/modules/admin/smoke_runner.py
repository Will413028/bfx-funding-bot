"""Admin smoke-test runner — synthesize paper offer through prod chain.

Boot-time auto + admin endpoint share SmokeRunner core (this file). Synthetic
events tagged account_id="smoke_test"; prod ledger filters them out via
account_id guard (modules/execution/ledger.py).

Design: docs/superpowers/specs/2026-05-22-admin-smoke-test-design.md
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Module-level single-flight lock. Both boot smoke and endpoint share it.
# Router pre-checks .locked() to return 409; SmokeRunner uses async-with to
# serialize (no contention expected because router pre-check filters).
_SMOKE_LOCK = asyncio.Lock()


@dataclass
class EventRecorder:
    """Spy that captures bus events matching a given account_id."""
    account_id: str
    events: list[Any] = field(default_factory=list)

    async def record(self, event: Any) -> None:
        if getattr(event, "account_id", None) == self.account_id:
            self.events.append(event)
