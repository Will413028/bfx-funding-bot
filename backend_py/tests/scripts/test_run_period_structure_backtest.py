"""Smoke: the period-structure runner produces a report from frozen fixtures."""
import asyncio
import json
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.fixture_io import freeze_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from scripts.run_period_structure_backtest import _amain

_HOUR = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z
_REPO = Path(__file__).resolve().parents[2]


def _series(period_agg: str, close: str, n: int) -> list[FundingCandle]:
    return [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg=period_agg,
                      mts=_T0 + i * _HOUR, close=Decimal(close))
        for i in range(n)
    ]


def test_runner_writes_markdown_and_json_from_fixtures(tmp_path: Path) -> None:
    n = 24 * 31 * 5
    fixtures = tmp_path / "candles"
    freeze_candles(
        {
            ("fUST", "p2", "1h"): _series("p2", "0.0001", n),
            ("fUST", "p30", "1h"): _series("p30", "0.0003", n),
            ("fUST", "a30", "1h"): _series("a30", "0.0002", n),
        },
        fixtures,
    )
    out = tmp_path / "report.md"
    rc = asyncio.run(_amain([
        "--fixtures", str(fixtures), "--symbols", "fUST", "--output", str(out),
        "--cells", str(_REPO / "configs/cells.yaml"),
        "--ap-cells", str(_REPO / "configs/cells.experimental-p14.yaml"),
    ]))
    assert rc == 0
    md = out.read_text()
    for arm in ("always_2d", "always_30d", "adaptive_period", "mr_a30", "mr_a30_legacy", "mr_p2"):
        assert f"| {arm} |" in md
    assert "| always_frr |" not in md, "no FRR arm offline"
    payload = json.loads(out.with_suffix(".json").read_text())
    sym = payload["symbols"][0]
    assert sym["symbol"] == "fUST"
    # One 30d lock per window, plus a second (truncated) one in 31-day months once the
    # 30d + gap cooldown expires -> at least one p30-priced trade per window, nothing else.
    assert set(sym["series_used"]["always_30d"]) == {"p30"}
    assert sym["series_used"]["always_30d"]["p30"] >= sym["arms"]["always_30d"]["n_windows"]
    assert "always_30d_vs_always_2d" in sym["pairs"]
    assert any("always_frr" in note for note in sym["notes"])
