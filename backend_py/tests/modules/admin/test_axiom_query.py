"""AxiomSmokeQueryAdapter — APL query for L3 round-trip verification."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from bfx_funding_bot.modules.admin.axiom_query import AxiomSmokeQueryAdapter


def _tabular_response(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an Axiom APL tabular response from a list of row dicts."""
    if not rows:
        return {"tables": []}
    fields = list(rows[0].keys())
    columns = [[r[f] for r in rows] for f in fields]
    return {
        "tables": [
            {"fields": [{"name": f} for f in fields], "columns": columns},
        ],
    }


async def test_query_returns_parsed_rows() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(200, json=_tabular_response([
            {"_time": "2026-05-22T10:00:00Z", "event_type": "reservation_claimed",
             "account_id": "smoke_test", "payload": {"size_usdt": 1.0}},
            {"_time": "2026-05-22T10:00:01Z", "event_type": "order_fill",
             "account_id": "smoke_test", "payload": {"fill_size_usdt": 1.0}},
        ]))

    transport = httpx.MockTransport(handler)
    adapter = AxiomSmokeQueryAdapter(
        api_key="key", dataset="ds", base_url="https://api.axiom.co",
    )
    adapter._http = httpx.AsyncClient(transport=transport, base_url="https://api.axiom.co",
                                       headers={"Authorization": "Bearer key"})

    rows = await adapter.query_order_events(
        "smoke_test", datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC),
    )
    assert len(rows) == 2
    assert rows[0]["event_type"] == "reservation_claimed"
    assert rows[1]["event_type"] == "order_fill"
    # APL string check
    assert "smoke_test" in captured["body"]
    assert "reservation_claimed" in captured["body"]
    assert "order_fill" in captured["body"]
    await adapter.aclose()


async def test_query_empty_returns_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tables": []})

    transport = httpx.MockTransport(handler)
    adapter = AxiomSmokeQueryAdapter(api_key="key", dataset="ds")
    adapter._http = httpx.AsyncClient(transport=transport, base_url="https://api.axiom.co",
                                       headers={"Authorization": "Bearer key"})
    rows = await adapter.query_order_events("smoke_test", datetime.now(UTC))
    assert rows == []
    await adapter.aclose()


async def test_query_raises_on_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    adapter = AxiomSmokeQueryAdapter(api_key="bad", dataset="ds")
    adapter._http = httpx.AsyncClient(transport=transport, base_url="https://api.axiom.co",
                                       headers={"Authorization": "Bearer bad"})
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.query_order_events("smoke_test", datetime.now(UTC))
    await adapter.aclose()
