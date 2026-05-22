"""Schema migration framework — read-side upcaster chain (Greg Young 2014).

Each upcaster transforms an Axiom row dict from schema v_n → v_n+1.
Replay path: row → upcast_row(row) → parse to Pydantic/dataclass → handler.

Migration rules:
  - Adding Optional fields with None default is permitted (upcaster fills None).
  - Removing fields is FORBIDDEN (breaks existing rows).
  - Renaming requires explicit upcaster (copy old → new, keep both Optional).

Phase 4.4a · v1→v2 adds: venue_seq, event_seq, occurred_at_ms, recorded_at_ms
"""
from __future__ import annotations

from collections.abc import Callable


def upcast_v1_to_v2(row: dict[str, object]) -> dict[str, object]:
    """4.3 row (no bitemporal/seq) → 4.4a row (with Optional bitemporal/seq fields).

    Uses .get() with None default — if field already present (4.4a row passing
    through), value is preserved.
    """
    return {
        **row,
        "venue_seq": row.get("venue_seq"),
        "event_seq": row.get("event_seq"),
        "occurred_at_ms": row.get("occurred_at_ms"),
        "recorded_at_ms": row.get("recorded_at_ms"),
    }


UPCASTER_CHAIN: list[Callable[[dict[str, object]], dict[str, object]]] = [
    upcast_v1_to_v2,
]


def upcast_row(row: dict[str, object]) -> dict[str, object]:
    """Apply full upcaster chain to a row. Idempotent (re-running is safe)."""
    for upcaster in UPCASTER_CHAIN:
        row = upcaster(row)
    return row
