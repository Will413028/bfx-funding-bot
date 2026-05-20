from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

from g1_smoke_check import (  # type: ignore[import-not-found]
    AxiomQueryClient,
    build_apl_query_c2,
    build_apl_query_c3,
    build_apl_query_c6,
    run_c1_continuity,
    run_c2_emit_completeness,
    run_c5_zero_error_health,
    run_c6_zero_divergence,
)


def test_apl_query_c2_includes_required_filters():
    q = build_apl_query_c2(phase="paper", dataset="evt", hours=1)
    assert "phase == 'paper'" in q
    assert "event_type" in q and "signal" in q


def test_apl_query_c3_uses_bracket_quoted_payload_field():
    q = build_apl_query_c3(
        phase="paper", dataset="evt", hours=1,
        strategy="mean_reversion", cell="fUSD_p2", lo=-1.0, hi=1.0,
    )
    assert "['payload.signal_score']" in q
    assert "payload.signal_score)" not in q


def test_apl_query_c6_uses_bracket_quoted_divergence_field():
    q = build_apl_query_c6(phase="paper", dataset="evt", hours=1)
    assert "['payload.divergence_detail.diff_fields']" in q


def test_tabular_to_rows_basic():
    payload = {
        "tables": [{
            "fields": [{"name": "strategy"}, {"name": "cell"}, {"name": "count_"}],
            "columns": [
                ["mean_reversion", "rate_percentile"],
                ["fUSD_p2", "fUST_a30"],
                [3, 5],
            ],
        }],
    }
    rows = AxiomQueryClient._tabular_to_rows(payload)
    assert rows == [
        {"strategy": "mean_reversion", "cell": "fUSD_p2", "count_": 3},
        {"strategy": "rate_percentile", "cell": "fUST_a30", "count_": 5},
    ]


def test_tabular_to_rows_empty_tables():
    assert AxiomQueryClient._tabular_to_rows({}) == []
    assert AxiomQueryClient._tabular_to_rows({"tables": []}) == []
    assert AxiomQueryClient._tabular_to_rows(
        {"tables": [{"fields": [{"name": "x"}], "columns": []}]},
    ) == []


async def test_c1_passes_when_all_buckets_non_empty():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(
        return_value=[{"_time": "t", "count_": 5} for _ in range(12)],
    )
    result = await run_c1_continuity(client=fake_client, phase="paper", hours=1)
    assert result.passed is True


async def test_c1_fails_with_empty_bucket():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=[
        *[{"_time": "t", "count_": 5} for _ in range(11)],
        {"_time": "t", "count_": 0},
    ])
    result = await run_c1_continuity(client=fake_client, phase="paper", hours=1)
    assert result.passed is False


async def test_c2_passes_with_expected_counts():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=[
        {"strategy": "mean_reversion", "cell": "fUSD_p2", "count_": 1},
    ])
    cells = [{"strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "p2", "timeframe": "1h"}]
    result = await run_c2_emit_completeness(
        client=fake_client, phase="paper", hours=1, cells=cells,
    )
    assert result.passed is True


async def test_c5_passes_with_zero_count():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=[{"count_": 0}])
    result = await run_c5_zero_error_health(client=fake_client, phase="paper", hours=1)
    assert result.passed is True


async def test_c5_passes_with_empty_rows():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=[])
    result = await run_c5_zero_error_health(client=fake_client, phase="paper", hours=1)
    assert result.passed is True


async def test_c6_fails_when_divergence_present():
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=[{"count_": 2}])
    result = await run_c6_zero_divergence(client=fake_client, phase="paper", hours=1)
    assert result.passed is False
    assert "2 divergence events" in result.detail
