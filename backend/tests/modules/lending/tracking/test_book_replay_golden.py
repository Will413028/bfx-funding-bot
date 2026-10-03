"""Golden characterization of `BookReplayLearner.learn` on frozen fixtures.

The expected output in `book_replay_golden.json` was recorded from the code as it
stood before the fill rule was extracted into `queue_fill`; this test pins that
research results do not move when the rule is shared with the simulated venue.
Inputs are deterministic: the committed fUST p2 close series, plus seeded
volumes and seeded synthetic ask snapshots (the fixtures carry no volume).
Regenerate only for a deliberate model change (bump `BOOK_MODEL_VERSION`).
"""
from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.fixture_io import load_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.book_replay import (
    BOOK_MODEL_VERSION,
    BookReplayLearner,
)
from bfx_funding_bot.modules.marketfeed.book_period_coverage import BookAskSnapshot

_BACKEND = Path(__file__).resolve().parents[4]
_FIXTURE = _BACKEND / "fixtures" / "candles" / "fUST_p2_1h.jsonl.gz"
_GOLDEN = Path(__file__).with_name("book_replay_golden.json")
_HOURS = 24 * 12
_SNAPSHOTS = 90


def golden_inputs() -> tuple[list[BookAskSnapshot], list[FundingCandle]]:
    rng = random.Random(20261004)
    base = load_candles(_FIXTURE)[:_HOURS]
    candles = [
        c.model_copy(update={"volume": Decimal(rng.randint(50, 4000))}) for c in base
    ]
    snapshots: list[BookAskSnapshot] = []
    for _ in range(_SNAPSHOTS):
        t = candles[0].mts + rng.randint(26, _HOURS - 30) * 3_600_000 + rng.choice(
            [0, 0, 37 * 60_000, 59_000]
        )
        ref = candles[0].close or Decimal("0.0002")
        asks = tuple(
            (
                ref * (Decimal(1) + Decimal(rng.randint(-150, 400)) / Decimal(10_000)),
                rng.choice([2, 2, 2, 3, 30]),
                Decimal(rng.randint(100, 6000)),
            )
            for _ in range(rng.randint(5, 25))
        )
        snapshots.append(BookAskSnapshot("fUST", t, asks))
    return snapshots, candles


def _encode(stats: list) -> list[dict[str, object]]:
    return [
        {
            "horizon_h": s.horizon_h,
            "spread_bucket_bps": s.spread_bucket_bps,
            "fill_prob": str(s.fill_prob),
            "n_samples": s.n_samples,
            "ttf_p50_ms": s.ttf_p50_ms,
            "ttf_p90_ms": s.ttf_p90_ms,
            "mean_ttf_ms": s.mean_ttf_ms,
        }
        for s in stats
    ]


def test_book_replay_output_matches_recorded_golden() -> None:
    snapshots, candles = golden_inputs()
    stats = BookReplayLearner(period_days=2).learn(snapshots, candles)
    expected = json.loads(_GOLDEN.read_text())
    assert BOOK_MODEL_VERSION == "book-replay-v2"
    assert len(expected) > 20
    assert _encode(stats) == expected
