from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.cell_derivation import derive_cell_params
from bfx_funding_bot.modules.backtest.cell_pipeline import (
    check_deployed_cells,
    check_main_against_fixture,
    write_outputs,
)
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.fixture_io import freeze_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel
from bfx_funding_bot.modules.marketfeed.config import load_cells_only
from bfx_funding_bot.modules.strategy import CellConfig

_LINEAR_CONFIG = BacktestConfig(fill_model="linear-baseline")
_UNUSED_LINEAR_MODEL = FillRateModel.from_rows([], artifact=None)


def _synthetic_series() -> list[FundingCandle]:
    # 8 months hourly; rate dips to 5% of base for 10h every 52h (same regime
    # as test_cell_derivation -> a selective combo beats passive; winner
    # ema_span=24, threshold_sigma=0.5).
    start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
    return [
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="a30",
            mts=start + i * 3_600_000,
            close=str(0.0004 * (0.05 if (i % 52) < 10 else 1.0)),
        )
        for i in range(24 * 30 * 8)
    ]


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    fixtures = tmp_path / "fixtures"
    candles = _synthetic_series()
    series = {("mean_reversion", "fUST", "a30"): candles}
    derived = {("mean_reversion", "fUST", "a30"): derive_cell_params(
        candles, config=_LINEAR_CONFIG, fill_model=_UNUSED_LINEAR_MODEL,
    )}
    cells_yaml = tmp_path / "cells.yaml"
    cells_yaml.write_text(
        "cells:\n"
        "  - strategy: mean_reversion\n"
        "    symbol: fUST\n"
        "    period_agg: a30\n"
        "    params:\n"
        "      threshold_sigma: 1.0\n"
        "      ratio_sigma: 0.5\n"
        "      ema_span: 168\n"
    )
    write_outputs(series, derived, fixtures, [cells_yaml], deployed_path=None)
    return fixtures, cells_yaml


def _loaded(path: Path) -> tuple[CellConfig, ...]:
    return tuple(load_cells_only(path))


def test_check_passes_on_freshly_written(tmp_path: Path) -> None:
    fixtures, cells_yaml = _setup(tmp_path)
    assert check_main_against_fixture(fixtures, cells_yaml, _loaded(cells_yaml), config=_LINEAR_CONFIG, fill_model=_UNUSED_LINEAR_MODEL)[0] == []


def test_check_fails_on_param_drift(tmp_path: Path) -> None:
    fixtures, cells_yaml = _setup(tmp_path)
    # Use ruamel to mutate the committed param so YAML stays parseable.
    from ruamel.yaml import YAML  # type: ignore[import-untyped]

    ruamel = YAML()
    doc = ruamel.load(cells_yaml.read_text())
    doc["cells"][0]["params"]["threshold_sigma"] = 9.9
    with cells_yaml.open("w") as fh:
        ruamel.dump(doc, fh)
    problems = check_main_against_fixture(fixtures, cells_yaml, _loaded(cells_yaml), config=_LINEAR_CONFIG, fill_model=_UNUSED_LINEAR_MODEL)[0]
    assert any("threshold_sigma" in p for p in problems)


def test_check_fails_on_fixture_hash_mismatch(tmp_path: Path) -> None:
    fixtures, cells_yaml = _setup(tmp_path)
    # Mutate the frozen fixture without updating _provenance.data_hash.
    freeze_candles(
        {
            ("fUST", "a30", "1h"): [
                FundingCandle(
                    symbol="fUST",
                    timeframe="1h",
                    period_agg="a30",
                    mts=i * 3_600_000,
                    close=Decimal("0.0009"),
                )
                for i in range(5)
            ]
        },
        fixtures,
    )
    problems = check_main_against_fixture(fixtures, cells_yaml, _loaded(cells_yaml), config=_LINEAR_CONFIG, fill_model=_UNUSED_LINEAR_MODEL)[0]
    assert any("data_hash" in p for p in problems)


def test_check_fails_on_deployed_param_drift(tmp_path: Path) -> None:
    fixtures, cells_yaml = _setup(tmp_path)
    # The deployed subset starts as a copy of the main file, then one param is diverged.
    from ruamel.yaml import YAML  # type: ignore[import-untyped]

    deployed_yaml = tmp_path / "cells.live.yaml"
    ruamel = YAML()
    doc = ruamel.load(cells_yaml.read_text())
    doc["cells"][0]["params"]["threshold_sigma"] = 9.9
    with deployed_yaml.open("w") as fh:
        ruamel.dump(doc, fh)
    problems, can_check_deployed = check_main_against_fixture(
        fixtures, cells_yaml, _loaded(cells_yaml),
        config=_LINEAR_CONFIG, fill_model=_UNUSED_LINEAR_MODEL,
    )
    assert can_check_deployed
    problems += check_deployed_cells(_loaded(cells_yaml), _loaded(deployed_yaml), cells_yaml.name)
    assert any("deployed" in p and "threshold_sigma" in p for p in problems)
