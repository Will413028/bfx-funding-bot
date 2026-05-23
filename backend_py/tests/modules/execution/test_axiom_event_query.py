"""Unit tests for AxiomReplayQueryAdapter pure functions + I/O shell."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from bfx_funding_bot.modules.execution.axiom_event_query import (
    AxiomReplayQueryAdapter,
)


def test_build_ledger_apl_includes_event_types_and_account_and_since() -> None:
    apl = AxiomReplayQueryAdapter._build_ledger_apl(
        dataset="bfx-events",
        account_id="default",
        since=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
    )
    assert "['bfx-events']" in apl
    assert "['account_id'] == 'default'" in apl
    assert "datetime(2026-05-01T12:00:00+00:00)" in apl
    assert "reservation_claimed" in apl
    assert "order_fill" in apl
    assert "reservation_released" in apl
    assert "order by _time asc" in apl
    # No `project` clause — Axiom flattens `payload` dict into dot-notation
    # columns; projecting `['payload']` returns HTTP 400 (4.4 prework lesson
    # commit 9261f3f). Parser handles dot-notation re-nest.
    assert "| project" not in apl


def test_build_registry_apl_no_account_filter_when_none() -> None:
    apl = AxiomReplayQueryAdapter._build_registry_apl(
        dataset="bfx-events",
        event_types=["reservation_claimed", "order_fill"],
        up_to_ms=None,
        account_id=None,
    )
    assert "['bfx-events']" in apl
    assert "['account_id']" not in apl  # no filter
    assert "reservation_claimed" in apl
    assert "order_fill" in apl
    assert "_time <=" not in apl  # no upper bound


def test_build_registry_apl_with_up_to_ms_and_account() -> None:
    apl = AxiomReplayQueryAdapter._build_registry_apl(
        dataset="bfx-events",
        event_types=["reservation_claimed"],
        up_to_ms=1700000000000,  # 2023-11-14T22:13:20Z
        account_id="acct-1",
    )
    assert "['account_id'] == 'acct-1'" in apl
    assert "_time <= datetime(2023-11-14T22:13:20+00:00)" in apl


def test_build_apl_escapes_quote_in_account_id_via_replacement() -> None:
    # Defensive: APL is single-quoted; embedded single-quote must not break query.
    # Implementation choice: reject account_id containing "'" (raise ValueError).
    import pytest
    with pytest.raises(ValueError, match="account_id"):
        AxiomReplayQueryAdapter._build_ledger_apl(
            dataset="bfx-events",
            account_id="bad'id",
            since=datetime(2026, 5, 1, tzinfo=UTC),
        )


def test_tabular_to_rows_handles_flat_payload_object_column() -> None:
    """Best case: Axiom returned `payload` as one nested object column."""
    payload_obj = {
        "cid": 1, "venue_offer_id": "v1", "size_usdt": 100.0,
        "signal_correlation_id": "uuid-1", "account_id": "default",
        "is_simulated": False,
    }
    response = {
        "tables": [{
            "fields": [
                {"name": "_time"}, {"name": "event_type"},
                {"name": "account_id"}, {"name": "correlation_id"},
                {"name": "payload"},
            ],
            "columns": [
                ["2026-05-23T10:00:00Z"],
                ["reservation_claimed"],
                ["default"],
                ["uuid-1"],
                [payload_obj],
            ],
        }],
    }
    rows = AxiomReplayQueryAdapter._tabular_to_rows(response)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "reservation_claimed"
    assert rows[0]["payload"] == payload_obj


def test_tabular_to_rows_re_nests_dot_notation_payload_columns() -> None:
    """Axiom flattens nested objects — `payload.cid` becomes a column.
    Adapter must re-nest these back into `payload` dict.

    Lesson source: 4.4 prework commit 9261f3f.
    """
    response = {
        "tables": [{
            "fields": [
                {"name": "_time"}, {"name": "event_type"},
                {"name": "account_id"}, {"name": "correlation_id"},
                {"name": "payload.cid"},
                {"name": "payload.venue_offer_id"},
                {"name": "payload.size_usdt"},
                {"name": "payload.signal_correlation_id"},
                {"name": "payload.account_id"},
                {"name": "payload.is_simulated"},
            ],
            "columns": [
                ["2026-05-23T10:00:00Z"],
                ["reservation_claimed"],
                ["default"],
                ["uuid-1"],
                [1],
                ["v1"],
                [100.0],
                ["uuid-1"],
                ["default"],
                [False],
            ],
        }],
    }
    rows = AxiomReplayQueryAdapter._tabular_to_rows(response)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "reservation_claimed"
    assert row["payload"]["cid"] == 1
    assert row["payload"]["venue_offer_id"] == "v1"
    assert row["payload"]["size_usdt"] == 100.0
    assert row["payload"]["is_simulated"] is False
    # Top-level fields preserved
    assert row["account_id"] == "default"


def test_tabular_to_rows_filters_none_payload_columns_from_union_schema() -> None:
    """Axiom's columnar schema is the UNION of all event types' payload fields.
    A row for order_fill includes `payload.X` columns from OTHER event types
    (e.g. signal_divergence's `payload.divergence_detail.replay.X`) with None
    value. These must be filtered, else downstream Pydantic `extra='forbid'`
    models reject the row.
    """
    response = {
        "tables": [{
            "fields": [
                {"name": "_time"}, {"name": "event_type"},
                {"name": "payload.cid"},
                {"name": "payload.offer_id"},
                {"name": "payload.fill_size_usdt"},
                # Bleed-through from signal_divergence event_type's schema:
                {"name": "payload.divergence_detail.replay.signal_direction"},
                {"name": "payload.divergence_detail.replay.signal_score"},
                {"name": "payload.signal_score"},
            ],
            "columns": [
                ["2026-05-23T10:00:00Z"],
                ["order_fill"],
                [1],
                ["v1"],
                [100.0],
                [None],  # null — order_fill doesn't have divergence_detail
                [None],
                [None],
            ],
        }],
    }
    rows = AxiomReplayQueryAdapter._tabular_to_rows(response)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    # Real fields kept
    assert payload == {"cid": 1, "offer_id": "v1", "fill_size_usdt": 100.0}
    # Null-bleed fields filtered (would crash OrderFillPayload `extra='forbid'`)
    assert "divergence_detail.replay.signal_direction" not in payload
    assert "divergence_detail.replay.signal_score" not in payload
    assert "signal_score" not in payload


def test_tabular_to_rows_empty_response_returns_empty_list() -> None:
    assert AxiomReplayQueryAdapter._tabular_to_rows({"tables": []}) == []
    assert AxiomReplayQueryAdapter._tabular_to_rows({}) == []
    assert AxiomReplayQueryAdapter._tabular_to_rows({
        "tables": [{"fields": [], "columns": []}],
    }) == []


def test_tabular_to_rows_multiple_rows() -> None:
    response = {
        "tables": [{
            "fields": [{"name": "_time"}, {"name": "event_type"}],
            "columns": [
                ["2026-05-23T10:00:00Z", "2026-05-23T11:00:00Z"],
                ["reservation_claimed", "order_fill"],
            ],
        }],
    }
    rows = AxiomReplayQueryAdapter._tabular_to_rows(response)
    assert len(rows) == 2
    assert rows[0]["event_type"] == "reservation_claimed"
    assert rows[1]["event_type"] == "order_fill"


@pytest.fixture
def fake_axiom_response() -> dict[str, Any]:
    return {
        "tables": [{
            "fields": [
                {"name": "_time"}, {"name": "event_type"},
                {"name": "account_id"}, {"name": "correlation_id"},
                {"name": "payload.cid"},
                {"name": "payload.venue_offer_id"},
                {"name": "payload.size_usdt"},
                {"name": "payload.signal_correlation_id"},
                {"name": "payload.account_id"},
                {"name": "payload.is_simulated"},
            ],
            "columns": [
                ["2026-05-23T10:00:00Z"],
                ["reservation_claimed"],
                ["default"],
                ["uuid-1"],
                [1], ["v1"], [100.0], ["uuid-1"], ["default"], [False],
            ],
        }],
    }


@pytest.mark.asyncio
async def test_query_order_events_posts_apl_and_returns_rows(
    fake_axiom_response: dict[str, Any],
) -> None:
    adapter = AxiomReplayQueryAdapter(api_key="test-key", dataset="bfx-events")
    try:
        with respx.mock(base_url="https://api.axiom.co") as router:
            route = router.post("/v1/datasets/_apl").mock(
                return_value=Response(200, json=fake_axiom_response),
            )
            rows = await adapter.query_order_events(
                "default", datetime(2026, 5, 1, tzinfo=UTC),
            )
        assert route.called
        request_body = route.calls.last.request.read().decode("utf-8")
        assert "reservation_claimed" in request_body
        assert "['account_id'] == 'default'" in request_body
        assert len(rows) == 1
        assert rows[0]["payload"]["cid"] == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_fetch_events_with_filters(
    fake_axiom_response: dict[str, Any],
) -> None:
    adapter = AxiomReplayQueryAdapter(api_key="test-key", dataset="bfx-events")
    try:
        with respx.mock(base_url="https://api.axiom.co") as router:
            router.post("/v1/datasets/_apl").mock(
                return_value=Response(200, json=fake_axiom_response),
            )
            rows = await adapter.fetch_events(
                event_types=["reservation_claimed"],
                up_to_ms=1700000000000,
                account_id="default",
            )
        assert len(rows) == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_query_raises_on_axiom_4xx() -> None:
    adapter = AxiomReplayQueryAdapter(api_key="bad-key", dataset="bfx-events")
    try:
        with respx.mock(base_url="https://api.axiom.co") as router:
            router.post("/v1/datasets/_apl").mock(
                return_value=Response(401, json={"error": "auth"}),
            )
            with pytest.raises(httpx.HTTPStatusError):
                await adapter.query_order_events("default", datetime.now(UTC))
    finally:
        await adapter.aclose()
