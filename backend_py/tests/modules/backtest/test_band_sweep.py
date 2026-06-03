# tests/modules/backtest/test_band_sweep.py
import math
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.band_sweep import enumerate_bands


def test_enumerate_bands_is_8_strict_pairs() -> None:
    bands = enumerate_bands()
    assert len(bands) == 8
    # strict t1 < t2, no (1.5, 1.5)
    assert all(t1 < t2 for t1, t2 in bands)
    assert (Decimal("1.5"), Decimal("1.5")) not in bands
    # the deployed candidate is in-grid
    assert (Decimal("0.5"), Decimal("1.5")) in bands
    # exact set
    assert set(bands) == {
        (Decimal("0.5"), Decimal("1.5")), (Decimal("0.5"), Decimal("2.0")), (Decimal("0.5"), Decimal("2.5")),
        (Decimal("1.0"), Decimal("1.5")), (Decimal("1.0"), Decimal("2.0")), (Decimal("1.0"), Decimal("2.5")),
        (Decimal("1.5"), Decimal("2.0")), (Decimal("1.5"), Decimal("2.5")),
    }
