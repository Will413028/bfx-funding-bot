"""Phase 4.4a integration: cold-start replay from Axiom rebuilds registry.

Phase 4.4a `_AxiomQueryAdapter` is a stub returning []. This test injects
a canned-rows axiom adapter to verify the replay → _parse_event → handle()
chain produces correct snapshot.

Note: OfferRegistry._parse_event only implements RESERVATION_CLAIMED in 4.4a.
Full replay activates with real adapter ADR in 4.4b.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.registry_offers import (
    OfferRegistry,
    RegistryState,
)


class _CannedRowsAxiom:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._rows)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cold_start_replay_rebuilds_registry_from_axiom_rows() -> None:
    sig_id = uuid4()
    voi = "42"
    rows = [
        {
            "event_type": "RESERVATION_CLAIMED",
            "occurred_at_ms": 1000, "event_seq": 1,
            "payload": {
                "cid": 42, "venue_offer_id": voi,
                "size_usdt": "100", "signal_correlation_id": str(sig_id),
                "account_id": "default", "is_simulated": False,
                "occurred_at_ms": 1000,
            },
        },
    ]

    registry = OfferRegistry(
        axiom_query=_CannedRowsAxiom(rows),
        clock=lambda: 5000,
    )

    await registry.replay_from_axiom()

    snap = registry.snapshot()
    assert voi in snap
    assert snap[voi].state == RegistryState.CLAIMED
    assert snap[voi].signal_correlation_id == sig_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cold_start_replay_with_empty_stub_no_op() -> None:
    """Phase 4.4a _AxiomQueryAdapter stub returns [] — replay no-op."""
    registry = OfferRegistry(
        axiom_query=_CannedRowsAxiom([]),
        clock=lambda: 5000,
    )
    await registry.replay_from_axiom()
    assert registry.snapshot() == {}
