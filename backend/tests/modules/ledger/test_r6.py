"""R6 uses local history bounds and identity presence, without venue-time tolerance."""

import pytest

from bfx_funding_bot.modules.ledger._internal.basis import _can_quarantine
from bfx_funding_bot.modules.ledger.tables import LedgerObservationRow


@pytest.mark.parametrize(
    ("start", "end", "history", "trades", "presence", "expected"),
    [
        (40_000, 100_000, True, True, None, True),
        (40_001, 100_000, True, True, None, False),  # one ms less than the 60 s margin
        (40_000, 99_999, True, True, None, False),
        (None, 100_000, True, True, None, False),
        (40_000, None, True, True, None, False),
        (40_000, 100_000, False, True, None, False),
        (40_000, 100_000, True, False, None, False),
        (40_000, 100_000, True, True, "active", False),
        (40_000, 100_000, True, True, "terminal", False),
        (40_000, 100_000, True, True, "trade", False),
    ],
)
def test_r6_requires_complete_evidence_and_exact_margin(
    start,
    end,
    history: bool,
    trades: bool,
    presence: str | None,
    expected: bool,
) -> None:
    observation = LedgerObservationRow(
        offer_history_complete=history,
        trades_complete=trades,
        trades_requested_start_ms=40_000,
        trades_requested_end_ms=100_000,
        history_requested_start_ms=start,
        history_requested_end_ms=end,
        evidence={"history_symbols": ["fUST"]},
    )
    assert (
        _can_quarantine(
            100_000,
            "gone",
            {"gone"} if presence == "active" else set(),
            {"gone"} if presence == "terminal" else set(),
            {"gone"} if presence == "trade" else set(),
            observation,
            "fUST",
        )
        is expected
    )


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (40_000, 100_000, True),
        (39_999, 100_001, True),
        (40_001, 100_000, False),
        (100_000, 100_000, False),
        (40_000, 99_999, False),
        (None, 100_000, False),
        (40_000, None, False),
    ],
)
def test_r6_requires_trades_requested_range(start, end, expected: bool) -> None:
    observation = LedgerObservationRow(
        offer_history_complete=True,
        trades_complete=True,
        history_requested_start_ms=40_000,
        history_requested_end_ms=100_000,
        trades_requested_start_ms=start,
        trades_requested_end_ms=end,
        evidence={"history_symbols": ["fUST"]},
    )
    assert _can_quarantine(100_000, "gone", (), (), (), observation, "fUST") is expected


@pytest.mark.parametrize(
    "evidence",
    [{}, {"history_symbols": None}, {"history_symbols": ["fUSD"]}, {"history_symbols": "fUST"}],
)
def test_r6_fails_closed_for_a_symbol_the_port_did_not_declare(evidence) -> None:
    """G1/R6: absence from a symbol's history proves nothing when none was fetched."""
    observation = LedgerObservationRow(
        offer_history_complete=True,
        trades_complete=True,
        history_requested_start_ms=40_000,
        history_requested_end_ms=100_000,
        trades_requested_start_ms=40_000,
        trades_requested_end_ms=100_000,
        evidence=evidence,
    )
    assert _can_quarantine(100_000, "gone", (), (), (), observation, "fUST") is False
