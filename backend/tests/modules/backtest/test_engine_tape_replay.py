"""run_backtest's tape-replay booking path: fills, lock start and idle time come from the tape."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import (
    BacktestIncomplete,
    _held_ms_until_refinance,
    run_backtest,
)
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


def _candles(n: int = 96) -> list[FundingCandle]:
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
    # Candles end at +95h: later decisions' 24h waits would pass it, so none is placed.
    write_day(tmp_path, "fUST", date(2026, 1, 12), [(3, T0 + 54 * H, -1000.0, 0.0002, 2)])
    write_day(tmp_path, "fUST", date(2026, 1, 13), [])
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
    assert result.idle_hours == Decimal("27")  # 2h wait + (1h to the close + 24h rest)
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


def test_no_placement_tail_stops_offers_earlier_than_the_wait(tmp_path: Path) -> None:
    for day in range(10, 14):
        write_day(tmp_path, "fUST", date(2026, 1, day), [])
    tape = TapeDays(tmp_path, "fUST")

    def trades(tail_h: int | None) -> int:
        config = BacktestConfig(fill_model="tape-replay", replay_max_wait_h=6,
                                replay_no_placement_tail_h=tail_h)
        return run_backtest(_candles(), _Fixed(), config, tape=tape).n_trades

    # Nothing fills: a decision every 9 candles (1h to the close + 6h wait + gap).
    # The window ends at +95h. Alone, the last offer must be placed by +89h (10 offers);
    # with a 24h tail, by +71h (8 offers).
    assert trades(None) == 10
    assert trades(24) == 8
    with pytest.raises(ValueError, match="replay_no_placement_tail_h"):
        BacktestConfig(fill_model="tape-replay", replay_max_wait_h=6,
                       replay_no_placement_tail_h=5)


def test_partly_filled_offer_counts_unfilled_share_of_lock_as_idle(tmp_path: Path) -> None:
    write_day(tmp_path, "fUST", date(2026, 1, 10), [(1, T0 + 2 * H, -250.0, 0.0003, 2)])
    write_day(tmp_path, "fUST", date(2026, 1, 11), [])
    write_day(tmp_path, "fUST", date(2026, 1, 12), [])
    write_day(tmp_path, "fUST", date(2026, 1, 13), [])
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


def test_decision_whose_wait_passes_window_end_is_not_placed(tape: TapeDays) -> None:
    # Window ends at +70h: the +52h decision (placed +53h, waits to +77h) is skipped
    # instead of booked as a market non-fill on a shortened wait.
    result = run_backtest(_candles(), _Fixed(), _config(), tape=tape,
                          record_end_mts=T0 + 70 * H)
    assert result.n_trades == 1
    assert result.fill_rate == Decimal("1")


def test_partial_lock_idle_is_clipped_to_window(tmp_path: Path) -> None:
    write_day(tmp_path, "fUST", date(2026, 1, 10), [(1, T0 + 2 * H, -250.0, 0.0003, 2)])
    write_day(tmp_path, "fUST", date(2026, 1, 11), [])
    # Window ends at +40h: the 48h lock starting at +25h has 15h inside it.
    result = run_backtest(_candles(), _Fixed(), _config(), tape=TapeDays(tmp_path, "fUST"),
                          record_end_mts=T0 + 40 * H)
    assert result.idle_hours == Decimal("25") + Decimal("0.75") * 15


def test_queue_provider_rejected_outside_replay() -> None:
    with pytest.raises(ValueError, match="same_rate_queue_ahead"):
        run_backtest(_candles(), _Fixed(), BacktestConfig(fill_model="linear-baseline"),
                     same_rate_queue_ahead=lambda offer: Decimal(0))


def _full_fill_tape(tmp_path: Path) -> TapeDays:
    """A trade above our rate every 2 hours over the four days: every offer fills at once."""
    for day in range(10, 14):
        day_ms = T0 + (day - 10) * 24 * H
        write_day(tmp_path, "fUST", date(2026, 1, day), [
            (day * 100 + k, day_ms + k * 2 * H + 1, -5000.0, 0.0003, 2) for k in range(12)
        ])
    return TapeDays(tmp_path, "fUST")


def _reference(drop_from_hour: int | None) -> list[FundingCandle]:
    """The 2-day market: at our rate, then half of it from `drop_from_hour` on."""
    return [FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=T0 + i * H,
                          close=RATE / 2 if drop_from_hour is not None and i >= drop_from_hour
                          else RATE)
            for i in range(96)]


def _refinance_config() -> BacktestConfig:
    return BacktestConfig(fill_model="tape-replay", replay_refinance_margin=Decimal("0.2"))


def test_borrower_repays_when_the_two_day_market_drops_below_the_margin(tmp_path: Path) -> None:
    tape = _full_fill_tape(tmp_path)
    held = run_backtest(_candles(), _Fixed(), _config(), tape=tape)
    # First offer: placed +1h, filled +2h, a 48h lock. The market halves at +10h, more
    # than 20% under the locked rate, so the borrower repays 8h into the lock.
    early = run_backtest(_candles(), _Fixed(), _refinance_config(), tape=tape,
                         refinance_reference=_reference(drop_from_hour=10))
    assert held.n_trades == 2
    # After the drop every new lock is repaid the next hour: many short locks.
    assert early.n_trades > 10
    assert early.gross_monthly_return_pct < held.gross_monthly_return_pct / 2


def test_no_drop_means_the_lock_is_held_to_maturity(tmp_path: Path) -> None:
    tape = _full_fill_tape(tmp_path)
    held = run_backtest(_candles(), _Fixed(), _config(), tape=tape)
    same = run_backtest(_candles(), _Fixed(), _refinance_config(), tape=tape,
                        refinance_reference=_reference(drop_from_hour=None))
    assert same.model_dump(exclude={"strategy_name"}) == held.model_dump(exclude={"strategy_name"})


def test_a_drop_inside_the_margin_does_not_trigger_repayment(tmp_path: Path) -> None:
    tape = _full_fill_tape(tmp_path)
    held = run_backtest(_candles(), _Fixed(), _config(), tape=tape)
    # Half the rate is a 50% drop: a 60% margin tolerates it.
    config = BacktestConfig(fill_model="tape-replay", replay_refinance_margin=Decimal("0.6"))
    same = run_backtest(_candles(), _Fixed(), config, tape=tape,
                        refinance_reference=_reference(drop_from_hour=10))
    assert same.n_trades == held.n_trades
    assert same.gross_monthly_return_pct == held.gross_monthly_return_pct


def test_refinance_margin_and_reference_come_together(tape: TapeDays) -> None:
    with pytest.raises(ValueError, match="refinance_reference"):
        run_backtest(_candles(), _Fixed(), _refinance_config(), tape=tape)
    with pytest.raises(ValueError, match="refinance_reference"):
        run_backtest(_candles(), _Fixed(), _config(), tape=tape,
                     refinance_reference=_reference(None))
    for bad in ("0", "1", "-0.1"):
        with pytest.raises(ValueError, match="replay_refinance_margin"):
            BacktestConfig(fill_model="tape-replay", replay_refinance_margin=Decimal(bad))


def _held(closes: list[str | None], *, lock_start: int, rate: str = "0.0002",
          term_h: int = 48) -> int:
    reference = ([T0 + i * H for i in range(len(closes))],
                 [Decimal(c) if c is not None else None for c in closes])
    return _held_ms_until_refinance(reference, lock_start_mts=lock_start,
                                    locked_rate=Decimal(rate), margin=Decimal("0.2"),
                                    term_ms=term_h * H)


def test_repayment_happens_when_the_low_close_is_known_not_when_its_hour_opens() -> None:
    flat, low = "0.0002", "0.0001"
    # Lock starts 1ms into hour 2. Hour 3 closes low at +4h: that is when the borrower
    # can act, 2h minus 1ms into the lock (not at +3h, when the close was still unknown).
    assert _held([flat, flat, flat, low, flat], lock_start=T0 + 2 * H + 1) == 2 * H - 1
    # The hour the lock starts in counts too: its close happens during the lock.
    assert _held([flat, flat, low, flat], lock_start=T0 + 2 * H + 1) == H - 1
    # A close before the lock started is history, not a trigger.
    assert _held([low, low, flat, flat], lock_start=T0 + 2 * H + 1, term_h=2) == 2 * H


def test_hours_without_a_print_do_not_trigger_repayment() -> None:
    flat = "0.0002"
    assert _held([flat, flat, None, "0", "-0.0001", flat], lock_start=T0 + H, term_h=4) == 4 * H


def test_a_lock_the_reference_does_not_cover_cannot_be_scored(tape: TapeDays) -> None:
    with pytest.raises(BacktestIncomplete) as exc:
        _held(["0.0002"], lock_start=T0 + 5 * H)
    assert exc.value.reason == "market_series_gap"
    with pytest.raises(ValueError, match="refinance_reference is empty"):
        run_backtest(_candles(), _Fixed(), _refinance_config(), tape=tape, refinance_reference=[])
