"""Round-trip test: emit 3 fake events to real Axiom, query them back.

Skipped unless AXIOM_API_KEY + AXIOM_DATASET are set in env.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from bfx_funding_bot.external.axiom import AxiomClient, AxiomConfig
from bfx_funding_bot.modules.execution.axiom_event_query import (
    AxiomReplayQueryAdapter,
)

pytestmark = pytest.mark.integration

_AXIOM_AVAIL = bool(os.environ.get("AXIOM_API_KEY")) and bool(
    os.environ.get("AXIOM_DATASET"),
)


@pytest.mark.skipif(not _AXIOM_AVAIL, reason="AXIOM creds not set")
@pytest.mark.asyncio
async def test_axiom_replay_round_trip() -> None:
    api_key = os.environ["AXIOM_API_KEY"]
    dataset = os.environ["AXIOM_DATASET"]
    test_account = f"itest-{uuid.uuid4().hex[:8]}"

    # 1. Emit 3 fake events
    client = AxiomClient(AxiomConfig(api_key=api_key, dataset=dataset))
    await client.start()
    try:
        now_iso = datetime.now(UTC).isoformat()
        for et, payload in [
            ("reservation_claimed", {
                "cid": 1, "venue_offer_id": "v1", "size_usdt": 100.0,
                "signal_correlation_id": str(uuid.uuid4()),
                "is_simulated": True,
            }),
            ("order_fill", {
                "cid": 1, "offer_id": "v1",
                "signal_correlation_id": str(uuid.uuid4()),
                "fill_size_usdt": 100.0, "fill_price": 0.0001,
                "is_simulated": True,
            }),
            ("reservation_released", {
                "cid": 1, "venue_offer_id": "v1", "size_usdt": 100.0,
                "reason": "user_cancel",
                "signal_correlation_id": str(uuid.uuid4()),
                "is_simulated": True,
            }),
        ]:
            await client.emit({
                "timestamp": now_iso,
                "level": "info",
                "phase": "paper",
                "strategy": "rate_percentile",
                "cell": "test",
                "event_type": et,
                "correlation_id": str(uuid.uuid4()),
                "account_id": test_account,
                "payload": payload,
            })
        await client.flush()
    finally:
        await client.stop()

    # Axiom indexing latency
    await asyncio.sleep(5)

    # 2. Query back
    adapter = AxiomReplayQueryAdapter(api_key=api_key, dataset=dataset)
    try:
        since = datetime.now(UTC) - timedelta(minutes=5)
        rows = await adapter.query_order_events(test_account, since)
    finally:
        await adapter.aclose()

    # 3. Assert all 3 returned
    event_types = sorted({r["event_type"] for r in rows})
    assert event_types == ["order_fill", "reservation_claimed", "reservation_released"]

    # Verify dot-notation payload re-nesting worked
    rc = next(r for r in rows if r["event_type"] == "reservation_claimed")
    assert "payload" in rc
    assert rc["payload"]["cid"] == 1
    assert rc["payload"]["venue_offer_id"] == "v1"
