"""The decision-space search runner: topics, trials guard, tape-limited windows."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.fixture_io import freeze_candles
from scripts.run_decision_space_search import (
    Grid,
    SearchSetup,
    _amain,
    _parse_args,
    prepare_symbol,
)
from scripts.run_oos_profitability import DEFAULT_N_TRIALS
from scripts.run_period_structure_backtest import ApParams
from tests.modules.backtest.test_replay import write_day

_HOUR = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z
_REPO = Path(__file__).resolve().parents[2]
_ENTRY = ["--registry-entry", "test-entry"]
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
    rc = asyncio.run(_amain([*_ENTRY, "--topic", "period-arms", *_base(*inputs, out)]))
    assert rc == 0
    payload = json.loads(out.with_suffix(".json").read_text())
    assert payload["n_trials"] == DEFAULT_N_TRIALS
    symbol = payload["symbols"][0]
    # June has candles but no tape: only April and May are scored.
    assert symbol["n_windows"] == 2
    assert set(symbol["verdicts"]) == {"always_30d", "mr_p2", "mr_a30", "adaptive_period"}
    # Two months are far below the 12 windows a verdict needs.
    assert set(symbol["verdicts"].values()) == {"too_few_windows"}
    assert symbol["n_configs"] == 4
    for bound in ("conservative", "optimistic"):
        assert set(symbol["bounds"][bound]["arms"]) == {
            "always_2d", "always_30d", "mr_p2", "mr_a30", "adaptive_period"}
        assert not any("not scored" in note for note in symbol["bounds"][bound]["notes"])
    markdown = out.read_text()
    assert "| always_30d |" in markdown
    assert "mr_a30_legacy" not in markdown.split("## fUST")[1].split("### Paired")[1]
    assert "2 of 3 WFO windows" in markdown
    assert "at least 12 scored windows" in markdown


def test_grid_topic_builds_one_arm_per_point(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    out = tmp_path / "grid.md"
    rc = asyncio.run(_amain([
        *_ENTRY, "--topic", "grid", "--offsets", "0,0.5", "--periods", "2,30", "--wait-hours", "6,24",
        "--n-trials", str(DEFAULT_N_TRIALS + 7), *_base(*inputs, out),
    ]))
    assert rc == 0
    symbol = json.loads(out.with_suffix(".json").read_text())["symbols"][0]
    # 2 x 2 x 2 points minus the baseline itself (offset 0, 2 days, 24h).
    assert symbol["n_configs"] == 7
    assert "mkt_p30_off0.5_w6" in symbol["verdicts"]
    assert "mkt_p2_off0_w24" not in symbol["verdicts"]
    means = {arm: Decimal(values["mean_monthly"])
             for arm, values in symbol["bounds"]["conservative"]["arms"].items()}
    # Every trade prints above every quote, so the 50% higher 2-day quote earns more.
    assert means["mkt_p2_off0.5_w24"] > means["always_2d"]
    # ...and a shorter wait that never binds earns the same as the baseline.
    assert means["mkt_p2_off0_w6"] == means["always_2d"]


def test_grid_size_excludes_the_baseline_point_and_duplicates() -> None:
    grid = Grid(offsets=(Decimal(0), Decimal("0.0"), Decimal("0.1")), periods=(2,),
                wait_hours=(24,))
    assert grid.size() == 1


def test_grid_refuses_an_undercounted_trials_number(inputs: tuple[Path, Path]) -> None:
    args = [*_ENTRY, "--topic", "grid", "--offsets", "0.1,0.2", "--periods", "2", "--wait-hours", "24",
            "--tape-root", "/nonexistent", "--output", "/nonexistent/x.md"]
    with pytest.raises(SystemExit):
        _parse_args([*args, "--n-trials", str(DEFAULT_N_TRIALS + 1)])
    assert _parse_args([*args, "--n-trials", str(DEFAULT_N_TRIALS + 2)]).n_trials == DEFAULT_N_TRIALS + 2


def test_grid_requires_an_explicit_trials_number() -> None:
    with pytest.raises(SystemExit):
        _parse_args([*_ENTRY, "--topic", "grid", "--offsets", "0.1", "--periods", "2", "--wait-hours", "24",
                     "--tape-root", "/nonexistent", "--output", "/nonexistent/x.md"])


@pytest.mark.parametrize("flags", [
    # `=` form: argparse reads a separate "-1,0.1" as an option, not a value.
    ["--offsets=-1,0.1", "--periods", "2", "--wait-hours", "24"],
    ["--offsets", "abc", "--periods", "2", "--wait-hours", "24"],
    ["--offsets", "0.1", "--periods", "1", "--wait-hours", "24"],
    ["--offsets", "0.1", "--periods", "121", "--wait-hours", "24"],
    ["--offsets", "0.1", "--periods", "2", "--wait-hours", "0"],
    ["--offsets", "0.1", "--periods", "2", "--wait-hours", "169"],  # would eat the month
    ["--offsets", "0", "--periods", "2", "--wait-hours", "24"],  # only the baseline
])
def test_grid_rejects_bad_values_before_loading_anything(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parse_args([*_ENTRY, "--topic", "grid", *flags, "--n-trials", "999",
                     "--tape-root", "/nonexistent", "--output", "/nonexistent/x.md"])


def test_grid_accepts_a_quote_below_market() -> None:
    args = _parse_args([*_ENTRY, "--topic", "grid", "--offsets=-0.02,0.02", "--periods", "2",
                        "--wait-hours", "24", "--n-trials", "999",
                        "--tape-root", "/nonexistent", "--output", "/nonexistent/x.md"])
    assert args.grid.offsets == (Decimal("-0.02"), Decimal("0.02"))


def _setup(tape: Path, grid: Grid | None) -> SearchSetup:
    return SearchSetup(tape_root=tape, grid=grid, n_trials=999, offer_amount=Decimal("1000"),
                       cells=[], ap_cells=[],
                       ap=ApParams(ema_span=24, t1=Decimal("0.5"), t2=Decimal("2.0"),
                                   p_mid=7, p_long=14),
                       p30_staleness_hours=12)


def test_prepare_fails_clearly_without_a_tape_or_a_series(inputs: tuple[Path, Path]) -> None:
    _fixtures, tape = inputs
    raw = {"p2": _series("p2", "0.0001", 24 * 183)}
    grid = Grid(offsets=(Decimal("0.1"),), periods=(7,), wait_hours=(24,))
    with pytest.raises(SystemExit, match="no market series for period 7"):
        prepare_symbol("fUST", raw, _setup(tape, grid))
    two_day = Grid(offsets=(Decimal("0.1"),), periods=(2,), wait_hours=(24,))
    with pytest.raises(SystemExit, match="no tape day files"):
        prepare_symbol("fUST", raw, _setup(tape / "elsewhere", two_day))


def test_grid_notes_say_which_series_and_tail_the_arms_used(inputs: tuple[Path, Path]) -> None:
    _fixtures, tape = inputs
    n = 24 * 183
    raw = {"p2": _series("p2", "0.0001", n), "a30": _series("a30", "0.0002", n),
           "p30": _series("p30", "0.0003", 24 * 100)}  # ends before any test month
    grid = Grid(offsets=(Decimal("0.1"),), periods=(2, 7), wait_hours=(6, 48))
    prepared = prepare_symbol("fUST", raw, _setup(tape, grid))
    assert "7-day arms quote off the a30 aggregate: no p7 series" in prepared.notes
    assert any(note.startswith("no offer is placed in the last 48h") for note in prepared.notes)
    # No arm observes p30, so its short history does not cost any window.
    assert set(prepared.series) == {"p2", "a30"}
    assert len(prepared.windows) == 2


def test_grid_topic_does_not_read_the_cells_files(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    fixtures, tape = inputs
    out = tmp_path / "grid.md"
    rc = asyncio.run(_amain([
        *_ENTRY, "--topic", "grid", "--offsets", "0.5", "--periods", "2", "--wait-hours", "24",
        "--n-trials", "999", "--fixtures", str(fixtures), "--tape-root", str(tape),
        "--symbols", "fUST", "--output", str(out), "--cells", str(tmp_path / "missing.yaml"),
    ]))
    assert rc == 0


def test_registered_trials_include_the_replayed_period_arms() -> None:
    # 38 before this runner + always_30d, mr_p2, mr_a30, adaptive_period on the replay.
    assert DEFAULT_N_TRIALS == 42


def _grid_run(
    inputs: tuple[Path, Path], out: Path, offsets: str, entry: str = "test-entry"
) -> dict[str, Any]:
    fixtures, tape = inputs
    rc = asyncio.run(_amain([
        "--registry-entry", entry, "--topic", "grid", "--offsets", offsets, "--periods", "2",
        "--wait-hours", "24", "--n-trials", "999", "--fixtures", str(fixtures),
        "--tape-root", str(tape), "--symbols", "fUST", "--output", str(out),
    ]))
    assert rc == 0
    record: dict[str, Any] = json.loads(out.with_suffix(".run.json").read_text())
    return record


def test_a_run_leaves_a_record_of_its_config_inputs_and_outputs(
    tmp_path: Path, inputs: tuple[Path, Path]
) -> None:
    record = _grid_run(inputs, tmp_path / "grid.md", "0.5")
    assert record["registry_entry"] == "test-entry"
    assert record["runner"] == "run_decision_space_search"
    assert record["code_version"]
    assert record["params"]["grid"] == {"offsets": ["0.5"], "periods": [2], "wait_hours": [24]}
    by_name = {item["name"]: item for item in record["inputs"]}
    # A 2-day grid reads p2 only: the loaded p30 and a30 series are not inputs of this run.
    assert set(by_name) == {"tape:fUST", "candles:fUST:p2"}
    assert by_name["tape:fUST"]["items"] == 152
    assert (by_name["tape:fUST"]["first"], by_name["tape:fUST"]["last"]) == (
        "fUST/2024-01-01.csv.gz", "fUST/2024-05-31.csv.gz")
    assert [o["name"] for o in record["outputs"]] == ["grid.md", "grid.json"]


def test_a_rerun_shares_the_config_hash_and_another_grid_or_entry_does_not(
    tmp_path: Path, inputs: tuple[Path, Path]
) -> None:
    first = _grid_run(inputs, tmp_path / "a.md", "0.5")
    rerun = _grid_run(inputs, tmp_path / "b.md", "0.50")
    other_grid = _grid_run(inputs, tmp_path / "c.md", "0.6")
    other_entry = _grid_run(inputs, tmp_path / "d.md", "0.5", entry="another-row")
    assert first["config_sha256"] == rerun["config_sha256"]
    assert first["inputs"] == rerun["inputs"]
    assert len({first["config_sha256"], other_grid["config_sha256"],
                other_entry["config_sha256"]}) == 3


def test_the_record_shows_when_the_tape_changed(tmp_path: Path, inputs: tuple[Path, Path]) -> None:
    _fixtures, tape = inputs
    before = _grid_run(inputs, tmp_path / "a.md", "0.5")
    write_day(tape, "fUST", date(2024, 5, 31), [(9_999_999, _T0 + 151 * 24 * _HOUR + 1, -1.0, 0.001, 2)])
    after = _grid_run(inputs, tmp_path / "b.md", "0.5")
    assert before["config_sha256"] == after["config_sha256"]
    tape_before, tape_after = (
        next(i for i in r["inputs"] if i["name"] == "tape:fUST") for r in (before, after))
    assert tape_before["sha256"] != tape_after["sha256"]


def test_a_run_without_a_registry_entry_is_refused() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--topic", "period-arms", "--tape-root", "/nonexistent",
                     "--output", "/nonexistent/x.md"])


def test_grid_order_and_repeats_do_not_change_the_config_hash(
    tmp_path: Path, inputs: tuple[Path, Path]
) -> None:
    one = _grid_run(inputs, tmp_path / "a.md", "0.5,0.6")
    other = _grid_run(inputs, tmp_path / "b.md", "0.6,0.5,0.50")
    assert one["config_sha256"] == other["config_sha256"]


def test_a_record_that_cannot_be_built_stops_the_run_before_any_report(
    tmp_path: Path, inputs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_decision_space_search as runner

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ValueError("fingerprint failed")

    monkeypatch.setattr(runner, "_input_fingerprints", refuse)
    out = tmp_path / "grid.md"
    with pytest.raises(ValueError, match="fingerprint failed"):
        _grid_run(inputs, out, "0.5")
    assert not out.exists()


@pytest.mark.parametrize("extra", [["--registry-entry", " padded "], ["--output", "/nonexistent/x.json"]])
def test_a_bad_registry_entry_or_output_is_refused_at_parse_time(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--registry-entry", "ok", "--topic", "period-arms", "--tape-root",
                     "/nonexistent", "--output", "/nonexistent/x.md", *extra])
