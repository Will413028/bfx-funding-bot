"""G3: Replay-from-axiom twice → identical projection state."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry


class _CannedAxiom:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_same_log_twice_same_state() -> None:
    sig_id = uuid4()
    rows = [
        {
            "event_type": "RESERVATION_CLAIMED",
            "occurred_at_ms": 1000, "event_seq": 1,
            "payload": {
                "cid": 42, "venue_offer_id": "42",
                "size_usdt": "100", "signal_correlation_id": str(sig_id),
                "account_id": "default", "is_simulated": False,
                "occurred_at_ms": 1000,
            },
        },
    ]

    r1 = OfferRegistry(axiom_query=_CannedAxiom(rows), clock=lambda: 5000)
    r2 = OfferRegistry(axiom_query=_CannedAxiom(rows), clock=lambda: 5000)

    await r1.replay_from_axiom()
    await r2.replay_from_axiom()

    s1 = r1.snapshot()
    s2 = r2.snapshot()
    assert s1 == s2
    # Determinism — correlation_id from log, NOT uuid4
    assert s1["42"].signal_correlation_id == sig_id
    assert s2["42"].signal_correlation_id == sig_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_with_shuffled_input_sorts_by_occurred_at_ms_and_event_seq() -> None:
    """Sort key is (occurred_at_ms, event_seq) — same final state regardless of input order."""
    sig_id = uuid4()
    base_payload = {
        "venue_offer_id": "42", "cid": 42, "size_usdt": "100",
        "signal_correlation_id": str(sig_id), "account_id": "default",
        "is_simulated": False,
    }
    rows_a = [
        {"event_type": "RESERVATION_CLAIMED", "occurred_at_ms": 1000, "event_seq": 1,
         "payload": {**base_payload, "occurred_at_ms": 1000}},
    ]
    rows_b = list(reversed(rows_a))

    r_a = OfferRegistry(axiom_query=_CannedAxiom(rows_a), clock=lambda: 5000)
    r_b = OfferRegistry(axiom_query=_CannedAxiom(rows_b), clock=lambda: 5000)

    await r_a.replay_from_axiom()
    await r_b.replay_from_axiom()
    assert r_a.snapshot() == r_b.snapshot()
