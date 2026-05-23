"""T12 — AxiomReplayQueryAdapter round-trip integration test.

Emits 3 events to the real Axiom CI dataset, polls until indexed, and asserts
the adapter parses them back with the deployment_environment / schema_version
envelope fields intact. Cross-run isolation via a GITHUB_RUN_ID-scoped
account_id (not a time window — the APL `_time >` filter uses a generous
lookback so ingestion-time jitter can't exclude freshly emitted events).

Skipped unless AXIOM_API_KEY + AXIOM_DATASET are set (CI sets them via the
backend_py_integration job; locally via docs/runbooks/observability-env-setup).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bfx_funding_bot.external.axiom import AxiomClient, AxiomConfig
from bfx_funding_bot.modules.execution.axiom_event_query import (
    AxiomReplayQueryAdapter,
)
from bfx_funding_bot.modules.observability.resource import (
    DeploymentEnvironment,
    EventResource,
)

pytestmark = pytest.mark.integration

_AXIOM_AVAIL = bool(os.environ.get("AXIOM_API_KEY")) and bool(
    os.environ.get("AXIOM_DATASET"),
)


async def _poll_until_indexed(
    adapter: AxiomReplayQueryAdapter,
    account_id: str,
    since: datetime,
    *,
    expected_count: int,
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
) -> list[dict[str, Any]]:
    """Poll Axiom every interval_s until expected_count events visible or timeout.

    Replaces the old fixed sleep(5) — Axiom indexing latency varies 2-15s
    under load, so a fixed sleep is both flaky (too short) and slow (too long).
    """
    deadline = time.monotonic() + timeout_s
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = await adapter.query_order_events(account_id, since)
        if len(rows) >= expected_count:
            return rows
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"Axiom indexing timeout: got {len(rows)}/{expected_count} after "
        f"{timeout_s}s (account_id={account_id})"
    )


@pytest.mark.skipif(not _AXIOM_AVAIL, reason="AXIOM creds not set")
@pytest.mark.asyncio
async def test_axiom_replay_round_trip() -> None:
    # Run-scoped isolation — parallel CI runs and local runs don't cross-pollute.
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    sha = os.environ.get("GITHUB_SHA", "local")[:8]
    run_account_id = f"ci-{run_id}-{sha}-{uuid.uuid4().hex[:6]}"
    # Generous lookback so the strict `_time >` APL filter comfortably includes
    # ingestion timestamps; account_id (above) provides the real isolation.
    since = datetime.now(UTC) - timedelta(minutes=5)

    cfg = AxiomConfig(
        api_key=os.environ["AXIOM_API_KEY"],
        dataset=os.environ["AXIOM_DATASET"],
        deployment_env=DeploymentEnvironment.CI,
        flush_interval_s=0.1,  # fast flush for the test
    )
    resource = EventResource(
        deployment_environment=DeploymentEnvironment.CI,
        service_version=os.environ.get("BFX_SERVICE_VERSION", "itest"),
    )
    client = AxiomClient(cfg=cfg, resource=resource)
    adapter = AxiomReplayQueryAdapter(
        api_key=cfg.api_key,
        dataset=cfg.dataset,
        deployment_environment=DeploymentEnvironment.CI,
    )

    now_iso = datetime.now(UTC).isoformat()
    await client.start()
    try:
        for event_type, payload in [
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
                "reason": "venue_cancel",
                "signal_correlation_id": str(uuid.uuid4()),
                "is_simulated": True,
            }),
        ]:
            await client.emit({
                "timestamp": now_iso,
                "event_type": event_type,
                "account_id": run_account_id,
                "payload": payload,
            })
        await client.flush()
    finally:
        await client.stop()

    try:
        rows = await _poll_until_indexed(
            adapter, run_account_id, since, expected_count=3, timeout_s=30,
        )
    finally:
        await adapter.aclose()

    # All 3 event types returned.
    event_types = sorted({r["event_type"] for r in rows})
    assert event_types == [
        "order_fill", "reservation_claimed", "reservation_released",
    ]

    # Envelope (resource) fields injected by AxiomClient.emit round-trip intact.
    assert all(r["deployment_environment"] == "ci" for r in rows), [
        r.get("deployment_environment") for r in rows
    ]
    assert all(int(r["schema_version"]) == 1 for r in rows)
    assert all(r["service_name"] == "bfx-funding-bot" for r in rows)
    assert all(r["service_version"] for r in rows)  # non-empty

    # Dot-notation payload re-nesting still works.
    rc = next(r for r in rows if r["event_type"] == "reservation_claimed")
    assert rc["payload"]["cid"] == 1
    assert rc["payload"]["venue_offer_id"] == "v1"
