import json
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from bfx_funding_bot.external.bitfinex.auth_ws import (
    AuthAck,
    BfxWSEvent,
    ChannelInfo,
    FccEvent,
    FcnEvent,
    FocEvent,
    Heartbeat,
    Unknown,
    parse_frame,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_fcn_credit_new() -> None:
    raw = _load("fcn_credit_new.json")
    result = parse_frame(raw)
    assert isinstance(result, FcnEvent)
    assert result.credit_id == 123456
    assert result.symbol == "fUSD"
    assert result.mts_create == 1716383500000
    assert result.amount == Decimal("100.0")
    assert result.rate == 0.0005
    assert result.period_days == 2


def test_parse_fcc_credit_close() -> None:
    """Shaped on credit 466642176 (2026-09-25), repaid after 14 minutes.

    Documented layout: RATE [11], PERIOD [12], MTS_OPENING [13], MTS_LAST_PAYOUT
    [14]. Rate and period were once read from [12]/[13] (a fixture built on that
    assumption kept this green), and the close time from MTS_UPDATE, which this
    frame leaves equal to MTS_CREATE.
    """
    raw = _load("fcc_credit_close.json")
    result = parse_frame(raw)
    assert isinstance(result, FccEvent)
    assert result.credit_id == 466642176
    assert result.symbol == "fUST"
    assert result.mts_create == 1790350246000
    assert result.mts_update == result.mts_create
    assert result.amount == Decimal("150.76884612")
    assert result.status == "CLOSED (used)"
    assert result.rate == 0.00019999
    assert result.period_days == 2
    assert result.mts_opening == 1790350246000
    assert result.mts_last_payout == 1790351088000  # close time, 14 min later


def test_parse_foc_canceled() -> None:
    raw = _load("foc_canceled.json")
    result = parse_frame(raw)
    assert isinstance(result, FocEvent)
    assert result.venue_offer_id == "789012"
    assert result.symbol == "fUSD"
    assert result.mts_create == 1716383500000
    assert result.mts_update == 1716383600000
    assert result.amount == Decimal("100.0")  # abs of signed -100.0 (venue signs offers negative)
    assert result.status == "CANCELED"
    assert result.rate == 0.0005
    assert result.period_days == 2


def test_parse_foc_executed() -> None:
    raw = _load("foc_executed.json")
    result = parse_frame(raw)
    assert isinstance(result, FocEvent)
    assert result.venue_offer_id == "789012"
    assert result.symbol == "fUSD"
    assert result.status.startswith("EXECUTED")
    assert result.rate == 0.0005
    assert result.period_days == 2


def test_parse_foc_malformed_row_is_dropped_not_raised() -> None:
    """A short/malformed foc array → parse_frame drops it (returns None), never
    raises (BitfinexShapeError must be caught like the other parse errors)."""
    raw = json.dumps([0, "foc", [789012, "fUSD", 1, 2]])  # < 16 elements
    assert parse_frame(raw) is None


def test_parse_auth_ack() -> None:
    raw = _load("auth_ack.json")
    result = parse_frame(raw)
    assert isinstance(result, AuthAck)
    assert result.status == "OK"


def test_parse_heartbeat() -> None:
    raw = _load("heartbeat.json")
    result = parse_frame(raw)
    assert isinstance(result, Heartbeat)


def test_parse_channel_info() -> None:
    raw = _load("channel_info.json")
    result = parse_frame(raw)
    assert isinstance(result, ChannelInfo)


def test_parse_invalid_json_returns_none() -> None:
    assert parse_frame("not-json{") is None


def test_parse_empty_returns_none() -> None:
    assert parse_frame("") is None


def test_parse_unknown_array_format_returns_unknown_or_none() -> None:
    raw = json.dumps([0, "xxx", [1, 2, 3]])
    result = parse_frame(raw)
    assert result is None or isinstance(result, Unknown)


@given(st.text(max_size=200))
def test_parse_frame_never_raises_on_random_input(raw: str) -> None:
    """G: parse never raises; returns None or typed BfxWSEvent."""
    result = parse_frame(raw)
    assert result is None or isinstance(result, BfxWSEvent)


def test_parse_fcc_zero_last_payout_is_unset() -> None:
    raw = _load("fcc_credit_close.json").replace("1790351088000", "0")
    result = parse_frame(raw)
    assert isinstance(result, FccEvent)
    assert result.mts_last_payout is None
