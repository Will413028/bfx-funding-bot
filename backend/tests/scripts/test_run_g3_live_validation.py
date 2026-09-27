"""The G3 CLI's argument parsing: --capital and the recorded --ack-week."""
from decimal import Decimal

import pytest

from scripts.run_g3_live_validation import _parse_acks, _parse_capital

_MON = 1789948800000   # 2026-09-21


def test_capital_must_be_positive():
    assert _parse_capital("570") == Decimal("570")
    assert _parse_capital(None) is None
    with pytest.raises(SystemExit):
        _parse_capital("0")


def test_ack_week_maps_a_monday_to_its_reason():
    assert _parse_acks(None) == {}
    assert _parse_acks(["2026-09-21=venue paid a late correction",
                        "2026-09-14 = checked ledger by hand "]) == {
        _MON: "venue paid a late correction",
        _MON - 7 * 86_400_000: "checked ledger by hand",
    }


@pytest.mark.parametrize("raw", [
    "2026-09-21",            # no reason: an ack must be recorded with one
    "2026-09-21=  ",
    "2026-09-22=not a Monday",
    "21/09/2026=bad date",
])
def test_ack_week_rejects_unrecorded_or_misdated_acks(raw):
    with pytest.raises(SystemExit):
        _parse_acks([raw])
