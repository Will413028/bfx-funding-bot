from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bfx_funding_bot.smoke.g1 import run_c1_continuity


def _cell(symbol="fUSD", period_agg="p2", strategy="mean_reversion", timeframe="1h"):
    return {
        "strategy": strategy,
        "symbol": symbol,
        "period_agg": period_agg,
        "timeframe": timeframe,
    }


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_c1_pass_regular_emissions():
    """11 cells each emit 1 signal in the hour, all within recency bound."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells_spec = [
        ("rate_percentile", "fUSD", "p2"),
        ("rate_percentile", "fUSD", "p30"),
        ("rate_percentile", "fUSD", "a30"),
        ("rate_percentile", "fUST", "p2"),
        ("rate_percentile", "fUST", "p30"),
        ("rate_percentile", "fUST", "a30"),
        ("mean_reversion", "fUSD", "p2"),
        ("mean_reversion", "fUSD", "p30"),
        ("mean_reversion", "fUSD", "a30"),
        ("mean_reversion", "fUST", "p2"),
        ("mean_reversion", "fUST", "a30"),
    ]
    cells = [_cell(strategy=s, symbol=sy, period_agg=p) for s, sy, p in cells_spec]

    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=30)),
            "strategy": c["strategy"],
            "cell": f"{c['symbol']}_{c['period_agg']}",
        }
        for c in cells
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail


@pytest.mark.asyncio
async def test_c1_fail_max_gap_exceeded():
    """1h cell with 91min gap between 2 signals exceeds 1.5x=90min bound."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]  # 1h
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=93)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
        {
            "_time": _iso(frozen_now - timedelta(minutes=2)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    assert "mean_reversion:fUSD_p2" in result.detail
    assert "max_gap" in result.detail


@pytest.mark.asyncio
async def test_c1_fail_recency_exceeded():
    """Last signal too old vs frozen_now."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]  # 1h, bound = 90min
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=91)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    assert "recency" in result.detail


@pytest.mark.asyncio
async def test_c1_fail_zero_signals_for_cell():
    """Cell config exists but no event emitted in window."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=[])

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    assert "0 signals" in result.detail


@pytest.mark.asyncio
async def test_c1_pass_single_signal_within_recency():
    """1 signal in window, age 30min < bound 90min, skip max-gap check."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=30)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail


@pytest.mark.asyncio
async def test_c1_pass_locf_cell_same_tolerance():
    """LOCF cell (with staleness_budget_hours) gets same 1.5x treatment."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [{
        "strategy": "rate_percentile",
        "symbol": "fUSD",
        "period_agg": "p30",
        "timeframe": "1h",
        "staleness_budget_hours": 12,  # LOCF override — should NOT affect C1
    }]
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=30)),
            "strategy": "rate_percentile", "cell": "fUSD_p30",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail


@pytest.mark.asyncio
async def test_c1_boundary_at_exact_tolerance():
    """gap = 90min exactly should pass (<=); 90min 1s should fail."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]  # bound = 90min = 5400s

    # Exactly 90min — pass
    events_pass = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=90)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
        {
            "_time": _iso(frozen_now),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events_pass)
    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail

    # 90min 1s — fail
    events_fail = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=90, seconds=1)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
        {
            "_time": _iso(frozen_now),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]
    fake_client.query_apl = AsyncMock(return_value=events_fail)
    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False

    # C1b recency boundary: single signal at exactly 90min before now — pass
    events_recency_pass = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=90)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]
    fake_client.query_apl = AsyncMock(return_value=events_recency_pass)
    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail

    # C1b recency boundary: single signal at 90min 1s before now — fail
    events_recency_fail = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=90, seconds=1)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]
    fake_client.query_apl = AsyncMock(return_value=events_recency_fail)
    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    assert "recency" in result.detail


@pytest.mark.asyncio
async def test_c1_fail_multi_cell_partial():
    """2 cells, one passes one fails — detail names only the failing cell."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [
        _cell(strategy="mean_reversion", symbol="fUSD", period_agg="p2"),     # will pass
        _cell(strategy="rate_percentile", symbol="fUST", period_agg="a30"),    # will fail (0 signals)
    ]
    # Only emit signal for the first cell
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=30)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    # Failing cell named in detail
    assert "rate_percentile:fUST_a30" in result.detail
    # Passing cell NOT named (no contamination)
    assert "mean_reversion:fUSD_p2" not in result.detail
