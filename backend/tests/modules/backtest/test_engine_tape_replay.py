"""run_backtest's tape-replay booking path: fills, lock start and idle time come from the tape."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import BacktestIncomplete, run_backtest
from bfx_funding_bot.modules.backtest.replay import TapeDays
from bfx_funding_bot.modules.strategy import LendDecision
from bfx_funding_bot.modules.strategy._internal.strategies.base import Strategy
from tests.modules.backtest.test_replay import write_day

H = 3_600_000
T0 = 1_768_003_200_000  # 2026-01-10T00:00:00Z
RATE = Decimal("0.0002")


class _Fixed(Strategy):
    @property
    def name(self) -> str:
        return "fixed"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        return LendDecision(mts=candle.mts, rate=RATE, period_days=2)


def _candles(n: int = 72) -> list[FundingCandle]:
    return [FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2",
                          mts=T0 + i * H, close=RATE) for i in range(n)]


@pytest.fixture
def tape(tmp_path: Path) -> TapeDays:
    # Offers are placed when the decision candle closes (+1h). The trade inside
    # the first decision hour (+30min) must not count. The first offer fills
    # fully at +2h through our rate and locks 48h, so the next decision is at
    # +52h (2h wait + 48 lock + 1 gap candle + 1).
    write_day(tmp_path, "fUST", date(2026, 1, 10), [
        (1, T0 + H // 2, -1000.0, 0.0003, 2),
        (2, T0 + 2 * H, -1000.0, 0.0003, 2),
    ])
    write_day(tmp_path, "fUST", date(2026, 1, 11), [])
    # The second offer (placed +53h) only meets a trade at exactly our rate.
    # Candles end at +71h, so its wait is capped at 18h and no 01-13 file is needed.
    write_day(tmp_path, "fUST", date(2026, 1, 12), [(3, T0 + 54 * H, -1000.0, 0.0002, 2)])
    return TapeDays(tmp_path, "fUST")


def _config(bound: Literal["conservative", "optimistic"] = "conservative") -> BacktestConfig:
    return BacktestConfig(fill_model="tape-replay", replay_bound=bound,
                          replay_offer_amount=Decimal("1000"))


def test_conservative_bound_books_tape_fills_and_idle_time(tape: TapeDays) -> None:
    result = run_backtest(_candles(), _Fixed(), _config(), tape=tape)
    assert result.model_kind == "tape-replay"
    assert result.replay_bound == "conservative"
    assert result.n_trades == 2
    assert result.fill_rate == Decimal("0.5")  # second offer unfilled
    assert result.idle_hours == Decimal("21")  # 2h wait + 1h candle + 18h capped rest
    assert result.gross_monthly_return_pct > 0


def test_optimistic_bound_counts_same_rate_trades(tape: TapeDays) -> None:
    result = run_backtest(_candles(), _Fixed(), _config("optimistic"), tape=tape)
    assert result.n_trades == 2
    assert result.fill_rate == Decimal("1")
    assert result.idle_hours == Decimal("4")


def test_missing_tape_day_fails_closed(tmp_path: Path, tape: TapeDays) -> None:
    (tmp_path / "fUST" / "2026-01-12.csv.gz").unlink()
    with pytest.raises(BacktestIncomplete) as exc:
        run_backtest(_candles(), _Fixed(), _config(), tape=TapeDays(tmp_path, "fUST"))
    assert exc.value.reason == "tape_day_missing"


def test_tape_replay_requires_tape_and_tape_requires_replay(tape: TapeDays) -> None:
    with pytest.raises(ValueError, match="requires tape"):
        run_backtest(_candles(), _Fixed(), _config())
    with pytest.raises(ValueError, match="only used"):
        run_backtest(_candles(), _Fixed(), BacktestConfig(fill_model="linear-baseline"),
                     tape=tape)


def test_partly_filled_offer_counts_unfilled_share_of_lock_as_idle(tmp_path: Path) -> None:
    write_day(tmp_path, "fUST", date(2026, 1, 10), [(1, T0 + 2 * H, -250.0, 0.0003, 2)])
    write_day(tmp_path, "fUST", date(2026, 1, 11), [])
    write_day(tmp_path, "fUST", date(2026, 1, 12), [])
    result = run_backtest(_candles(), _Fixed(), _config(), tape=TapeDays(tmp_path, "fUST"))
    assert result.fill_rate == Decimal("0.25")
    # 1h to the close + 24h wait, then 75% of the 48h lock idle.
    assert result.idle_hours == Decimal("61")


def test_queue_provider_feeds_queue_fill_rate(tape: TapeDays) -> None:
    result = run_backtest(_candles(), _Fixed(), _config(), tape=tape,
                          same_rate_queue_ahead=lambda offer: Decimal("400"))
    # offer 1 fills fully above our rate; offer 2 gets 1000 - 400 of the same-rate trade.
    assert result.queue_fill_rate == Decimal("0.8")
    assert result.fill_rate == Decimal("0.5")  # booking still uses the chosen bound


def test_replay_rejects_unused_pricing_inputs(tape: TapeDays) -> None:
    with pytest.raises(ValueError, match="no fill models or market series"):
        run_backtest(_candles(), _Fixed(), _config(), tape=tape, market_candles=_candles())


def test_replay_bound_is_validated_at_runtime() -> None:
    with pytest.raises(ValueError, match="replay_bound"):
        BacktestConfig(fill_model="tape-replay", replay_bound=cast(Any, "conservativ"))
