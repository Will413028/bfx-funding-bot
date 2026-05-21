from __future__ import annotations

from unittest.mock import AsyncMock

from bfx_funding_bot.smoke.g1 import (
    AxiomQueryClient,
    build_apl_query_c2,
    build_apl_query_c3,
    build_apl_query_c6,
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


def test_apl_query_c6_filters_signal_divergence_event_type():
    """C6 identifies divergences via dedicated event_type (orthogonal to severity),
    not by overloading level + payload existence."""
    q = build_apl_query_c6(phase="paper", dataset="evt", hours=1)
    assert "['event_type'] == 'signal_divergence'" in q
    assert "['level'] == 'warn'" not in q
    assert "isnotnull" not in q


def test_apl_query_c2_counts_signal_event_type_only():
    """C2 emit completeness: divergence events use distinct event_type, so
    filtering by event_type='signal' alone already excludes them — no level
    filter needed."""
    q = build_apl_query_c2(phase="paper", dataset="evt", hours=1)
    assert "['event_type'] == 'signal'" in q
    assert "signal_divergence" not in q


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
