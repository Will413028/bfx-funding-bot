"""G5: replay_from_axiom(up_to_ms=T) reconstructs state at point T."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry


class _PointInTimeAxiom:
    """Stub respects up_to_ms filter — returns only rows ≤ threshold."""
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch_events(
        self, *, up_to_ms: int | None = None, **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if up_to_ms is None:
            return list(self._rows)
        return [r for r in self._rows if (r.get("occurred_at_ms") or 0) <= up_to_ms]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_up_to_ms_returns_historical_state() -> None:
    sig_id = uuid4()
    rows = [
        # Event 1 at t=1000
        {"event_type": "RESERVATION_CLAIMED", "occurred_at_ms": 1000, "event_seq": 1,
         "payload": {"cid": 1, "venue_offer_id": "v1", "size_usdt": "100",
                     "signal_correlation_id": str(sig_id), "account_id": "default",
                     "is_simulated": False, "occurred_at_ms": 1000}},
        # Event 2 at t=2000
        {"event_type": "RESERVATION_CLAIMED", "occurred_at_ms": 2000, "event_seq": 2,
         "payload": {"cid": 2, "venue_offer_id": "v2", "size_usdt": "50",
                     "signal_correlation_id": str(uuid4()), "account_id": "default",
                     "is_simulated": False, "occurred_at_ms": 2000}},
    ]
    axiom = _PointInTimeAxiom(rows)

    # Full history replay → both offers present
    r_full = OfferRegistry(axiom_query=axiom, clock=lambda: 5000)
    await r_full.replay_from_axiom()
    assert "v1" in r_full.snapshot()
    assert "v2" in r_full.snapshot()

    # Replay up_to=1500 → only v1 present (time travel)
    r_t1500 = OfferRegistry(axiom_query=axiom, clock=lambda: 5000)
    await r_t1500.replay_from_axiom(up_to_ms=1500)
    assert "v1" in r_t1500.snapshot()
    assert "v2" not in r_t1500.snapshot()
