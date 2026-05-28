from decimal import Decimal

from bfx_funding_bot.modules.backtest.fixture_io import (
    fixture_data_hash,
    freeze_candles,
    load_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _series(close="0.0003"):
    return [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="a30",
                      mts=i * 3_600_000, close=Decimal(close))
        for i in range(5)
    ]


def test_freeze_load_round_trip_preserves_close_exactly(tmp_path):
    series = {("fUST", "a30", "1h"): _series("0.00031234567890123")}
    freeze_candles(series, tmp_path)
    loaded = load_candles(tmp_path / "fUST_a30_1h.jsonl.gz")
    assert len(loaded) == 5
    assert loaded[0].close == Decimal("0.00031234567890123")
    assert loaded[0].symbol == "fUST" and loaded[0].period_agg == "a30"
    assert [c.mts for c in loaded] == [i * 3_600_000 for i in range(5)]


def test_data_hash_deterministic_and_sensitive(tmp_path):
    freeze_candles({("fUST", "a30", "1h"): _series("0.0003")}, tmp_path)
    h1 = fixture_data_hash(tmp_path)
    freeze_candles({("fUST", "a30", "1h"): _series("0.0003")}, tmp_path)
    assert fixture_data_hash(tmp_path) == h1  # deterministic
    freeze_candles({("fUST", "a30", "1h"): _series("0.0009")}, tmp_path)
    assert fixture_data_hash(tmp_path) != h1  # content-sensitive
