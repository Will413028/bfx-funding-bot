"""The states of a legacy offer claim (``offer_claims.state``), as the frozen table holds them.

The in-memory offer registry that drove these transitions is gone with the legacy runtime
(S1-8). The enum stays for the readers of the frozen ``offer_claims`` table: the event-store
replay that the baseline restore drill runs, and the switch scaffolding (the capital
comparison closure, the seed); it goes with them.
"""
from __future__ import annotations

from enum import Enum


class RegistryState(Enum):
    PENDING = "pending"   # A2 write-ahead intent — durable, voi unknown
    UNKNOWN = "unknown"   # submit may have reached venue; quarantine until reconcile
    CLAIMED = "claimed"
    RELEASED = "released"
    FAILED = "failed"     # A2 submit-failed terminal


__all__ = ["RegistryState"]
