"""The decision-space search runner: topics, trials guard, tape-limited windows."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.fixture_io import freeze_candles
from scripts.run_decision_space_search import Grid, _amain, _parse_args
from scripts.run_oos_profitability import DEFAULT_N_TRIALS
from tests.modules.backtest.test_replay import write_day

_HOUR = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z
_REPO = Path(__file__).resolve().parents[2]
_CELLS = ["--cells", str(_REPO / "configs/cells.yaml"),
          "--ap-cells", str(_REPO / "configs/cells.experimental-p14.yaml")]


def _series(period_agg: str, close: str, n: int) -> list[FundingCandle]:
    return [FundingCandle(symbol="fUST", timeframe="1h", period_agg=period_agg,
                          mts=_T0 + i * _HOUR, close=Decimal(close)) for i in range(n)]


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Candles through 2024-07-01; a tape of trades above every quote through 2024-05-31."""
    n = 24 * 183
    fixtures = tmp_path / "candles"
    freeze_candles(
        {
            ("fUST", "p2", "1h"): _series("p2", "0.0001", n),
            ("fUST", "p30", "1h"): _series("p30", "0.0003", n),
            ("fUST", "a30", "1h"): _series("a30", "0.0002", n),
        },
        fixtures,
    )
    tape = tmp_path / "tape"
    for d in range(152):
        day_ms = _T0 + d * 24 * _HOUR
        write_day(tape, "fUST", date(2024, 1, 1) + timedelta(days=d), [
            (d * 10 + k, day_ms + k * 6 * _HOUR + 1, -5000.0, 0.001, 2) for k in range(4)
        ])
    return fixtures, tape


def _base(fixtures: Path, tape: Path, out: Path) -> list[str]:
    return ["--fixtures", str(fixtures), "--tape-root", str(tape), "--symbols", "fUST",
            "--output", str(out), *_CELLS]


def test_period_arms_topic_scores_both_bounds_inside_the_tape(
    tmp_path: Path, inputs: tuple[Path, Path]
) -> None:
    out = tmp_path / "report.md"
    rc = asyncio.run(_amain(["--topic", "period-arms", *_base(*inputs, out)]))
    assert rc == 0
    payload = json.loads(out.with_suffix(".json").read_text())
    assert payload["n_trials"] == DEFAULT_N_TRIALS
    symbol = payload["symbols"][0]
    # June has candles but no tape: only April and May are scored.
    assert symbol["n_windows"] == 2
    assert set(symbol["verdicts"]) == {"always_30d", "mr_p2", "mr_a30", "adaptive_period"}
    assert symbol["n_configs"] == 4
    for bound in ("conservative", "optimistic"):
        assert set(symbol["bounds"][bound]["arms"]) == {
            "always_2d", "always_30d", "mr_p2", "mr_a30", "adaptive_period"}
        assert not any("not scored" in note for note in symbol["bounds"][bound]["notes"])
    markdown = out.read_text()
    assert "| always_30d |" in markdown
    assert "mr_a30_legacy" not in markdown.split("## fUST")[1].split("### Paired")[1]
    assert "2 of 3 WFO windows" in markdown


def test_grid_topic_builds_one_arm_per_point(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    out = tmp_path / "grid.md"
    rc = asyncio.run(_amain([
        "--topic", "grid", "--offsets", "0,0.5", "--periods", "2,30", "--wait-hours", "6,24",
        "--n-trials", str(DEFAULT_N_TRIALS + 7), *_base(*inputs, out),
    ]))
    assert rc == 0
    symbol = json.loads(out.with_suffix(".json").read_text())["symbols"][0]
    # 2 x 2 x 2 points minus the baseline itself (offset 0, 2 days, 24h).
    assert symbol["n_configs"] == 7
    assert "mkt_p30_off0.5_w6" in symbol["verdicts"]
    assert "mkt_p2_off0_w24" not in symbol["verdicts"]
    # Every trade prints above every quote, so the 50% higher 2-day quote earns more.
    assert symbol["verdicts"]["mkt_p2_off0.5_w24"] == "positive_both_bounds"


def test_grid_size_excludes_the_baseline_point_and_duplicates() -> None:
    grid = Grid(offsets=(Decimal(0), Decimal("0.0"), Decimal("0.1")), periods=(2,),
                wait_hours=(24,))
    assert grid.size() == 1


def test_grid_refuses_an_undercounted_trials_number(inputs: tuple[Path, Path]) -> None:
    args = ["--topic", "grid", "--offsets", "0.1,0.2", "--periods", "2", "--wait-hours", "24",
            "--tape-root", "/nonexistent", "--output", "/nonexistent/x.md"]
    with pytest.raises(SystemExit):
        _parse_args([*args, "--n-trials", str(DEFAULT_N_TRIALS + 1)])
    assert _parse_args([*args, "--n-trials", str(DEFAULT_N_TRIALS + 2)]).n_trials == DEFAULT_N_TRIALS + 2


def test_grid_requires_an_explicit_trials_number() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--topic", "grid", "--offsets", "0.1", "--periods", "2", "--wait-hours", "24",
                     "--tape-root", "/nonexistent", "--output", "/nonexistent/x.md"])


def test_registered_trials_include_the_replayed_period_arms() -> None:
    # 38 before this runner + always_30d, mr_p2, mr_a30, adaptive_period on the replay.
    assert DEFAULT_N_TRIALS == 42
