"""Fuzz the Bitfinex REST response parsers: any decoded JSON body either parses or
raises the error the caller handles, never an unhandled exception.

Each parser's defined error is the one its callers catch: ``BitfinexShapeError`` for
the row parsers, ``InvariantViolation`` for the cancel-all classifier, and none at all
for the submit classification (once transport started, every answer is an outcome)
and the venue-error readers (they return None). ``BitfinexREST`` turns
``ROW_CONVERSION_ERRORS`` from the candle and stat rows into ``BitfinexShapeError``.

Defects the fuzz found (F40-F43) keep their shrunk counterexamples as regression cases.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bfx_funding_bot.external.bitfinex.auth_rest import (
    parse_active_funding_credits,
    parse_active_funding_offers,
    parse_funding_credit_history,
    parse_funding_trades,
    parse_interest_payments,
    parse_key_permissions,
    parse_user_id,
    parse_wallets,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError, parsed_venue_error
from bfx_funding_bot.external.bitfinex.funding_book_level import FundingBookLevel
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row
from bfx_funding_bot.external.bitfinex.funding_stat_row import FundingStat
from bfx_funding_bot.external.bitfinex.live_executor import (
    classify_cancel_all_response,
    parse_offer_response,
)
from bfx_funding_bot.external.bitfinex.observations import (
    parse_credit_observations,
    parse_offer_observations,
    parse_wallet_observations,
)
from bfx_funding_bot.external.bitfinex.rest import ROW_CONVERSION_ERRORS, FundingTrade
from bfx_funding_bot.external.bitfinex.submit_outcomes import (
    SubmitOutcomeUnknown,
    classify_submit_response,
)
from bfx_funding_bot.external.bitfinex.submit_wire import venue_error
from bfx_funding_bot.modules.execution.errors import InvariantViolation

# Values a Bitfinex row slot can hold after json.loads (which also yields inf and
# nan), plus the strings that are hardest on numeric conversion.
_NUMERIC_STRINGS = st.sampled_from(
    ["0", "1", "-1.5", "1e999", "-1e999", "nan", "inf", "-inf", "sNaN", "", " 2 ", "0x10"],
)
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=20),
    _NUMERIC_STRINGS,
)
_JSON = st.recursive(
    _SCALARS,
    lambda inner: st.lists(inner, max_size=6) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=30,
)
# A row is a list of slots. The length is drawn uniformly so rows long enough to pass a
# parser's length check (up to 16 slots) are common, not the rare tail of st.lists.
_ROW = st.integers(0, 24).flatmap(
    lambda n: st.lists(st.one_of(_SCALARS, _JSON), min_size=n, max_size=n),
)
# A body is an arbitrary JSON value, or the list of rows the endpoints answer with.
_BODY = st.one_of(_JSON, st.lists(_ROW, max_size=4), _ROW)

ROW_PARSERS: dict[str, Callable[[Any], object]] = {
    "active_funding_offers": parse_active_funding_offers,
    "active_funding_credits": parse_active_funding_credits,
    "wallets": parse_wallets,
    "key_permissions": parse_key_permissions,
    "user_id": parse_user_id,
    "interest_payments": parse_interest_payments,
    "funding_credit_history": parse_funding_credit_history,
    "funding_trades": parse_funding_trades,
    "funding_offer_row": parse_funding_offer_row,
    "wallet_observations": parse_wallet_observations,
    "offer_observations": parse_offer_observations,
    "credit_observations": lambda raw: parse_credit_observations(raw, source_kind="credit"),
    "public_funding_trade": FundingTrade.from_bitfinex,
    "public_funding_book_level": FundingBookLevel.from_bitfinex,
}


@pytest.mark.parametrize("parse", ROW_PARSERS.values(), ids=ROW_PARSERS.keys())
@given(raw=_BODY)
def test_row_parsers_raise_only_shape_errors(parse: Callable[[Any], object], raw: Any) -> None:
    with contextlib.suppress(BitfinexShapeError):
        parse(raw)


# What BitfinexREST turns into BitfinexShapeError from the candle and stat rows.
_CLIENT_WRAPS = ROW_CONVERSION_ERRORS


def _candle(raw: Any) -> object:
    return FundingCandle.from_bitfinex(raw, symbol="fUSD", timeframe="1h", period_agg="p2")


def _stat(raw: Any) -> object:
    return FundingStat.from_bitfinex(raw, symbol="fUSD")


@pytest.mark.parametrize("parse", [_candle, _stat], ids=["public_candle", "public_stat"])
@given(raw=_ROW)
def test_public_candle_and_stat_rows_raise_only_what_the_client_wraps(
    parse: Callable[[Any], object], raw: Any,
) -> None:
    with contextlib.suppress(*_CLIENT_WRAPS):
        parse(raw)


@given(raw=_BODY)
def test_cancel_all_response_raises_only_invariant_violation(raw: Any) -> None:
    with contextlib.suppress(InvariantViolation):
        classify_cancel_all_response(raw)


@given(raw=_BODY)
def test_venue_error_readers_never_raise(raw: Any) -> None:
    venue_error(raw)
    parsed_venue_error(json.dumps(raw))


@given(text=st.text(max_size=200))
def test_parsed_venue_error_never_raises_on_raw_text(text: str) -> None:
    parsed_venue_error(text)


# The counterexamples the fuzz shrank for F40-F42, kept so each fix is checked on
# every run, not only when the fuzz happens to draw a similar body.
INF = float("inf")
FORMER_DEFECTS = [
    pytest.param(parse_active_funding_credits, [[None] * 13], BitfinexShapeError, id="F40-credits"),
    pytest.param(parse_wallets, [["funding", "UST", "x", 0, 0]], BitfinexShapeError, id="F40-wallets"),
    pytest.param(parse_key_permissions, [[None, INF, None]], BitfinexShapeError, id="F40-permissions"),
    pytest.param(parse_interest_payments, [[None, None, None, False, None, None, None, None, None]],
                 BitfinexShapeError, id="F40-interest_payments"),
    pytest.param(parse_funding_credit_history,
                 [[False, False, None, False, False, False, None, None, None, None, None, False, False,
                   None, None]], BitfinexShapeError, id="F40-credit_history"),
    pytest.param(parse_funding_trades, [[False] * 7], BitfinexShapeError, id="F40-funding_trades"),
    pytest.param(FundingTrade.from_bitfinex, [False, INF, None, None, None], BitfinexShapeError,
                 id="F41-public_funding_trade"),
    pytest.param(FundingBookLevel.from_bitfinex, [None, None, None, None], BitfinexShapeError,
                 id="F41-public_funding_book_level"),
    pytest.param(_candle, [1, "x", 1, 1, 1, 1], ROW_CONVERSION_ERRORS, id="F42-public_candle"),
    pytest.param(_stat, [1, 0, 0, "x", 0, 0, 0, 0, 0, 0, 0, 0], ROW_CONVERSION_ERRORS,
                 id="F42-public_stat"),
    pytest.param(_stat, [INF, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], ROW_CONVERSION_ERRORS,
                 id="F42-public_stat_inf_mts"),
]


@pytest.mark.parametrize(("parse", "raw", "defined"), FORMER_DEFECTS)
def test_former_defect_raises_its_defined_error(
    parse: Callable[[Any], object], raw: Any, defined: type[Exception] | tuple[type[Exception], ...],
) -> None:
    with pytest.raises(defined):
        parse(raw)


# The submit boundary: once the request may have reached the venue, every answer is
# classified, never raised. An exception there would end the process (the command
# gate fences an outcome it cannot persist) and cost a restart plus an UNKNOWN.
_HTTP_STATUS = st.one_of(
    st.none(), st.sampled_from([200, 201, 204, 400, 401, 403, 404, 409, 422, 429, 500, 502, 503]),
    st.integers(-1, 700),
)


@given(status=_HTTP_STATUS, raw=_BODY)
def test_submit_classification_never_raises_after_transport(status: int | None, raw: Any) -> None:
    classify_submit_response(http_status=status, parsed_body=raw, transport_started=True)


@given(raw=_BODY)
def test_submit_response_parser_never_raises(raw: Any) -> None:
    parse_offer_response(raw)


@pytest.mark.parametrize(("status", "raw", "reason"), [
    # F43: a list or dict in the STATUS slot raised TypeError in the rejection check.
    pytest.param(200, [None, None, None, None, None, None, []], "malformed_response", id="F43-2xx"),
    pytest.param(401, [{}, "x", None, False, None, "x", {}], "unrecognized_response", id="F43-4xx"),
])
def test_unhashable_status_slot_is_unknown(status: int, raw: list[Any], reason: str) -> None:
    outcome = classify_submit_response(http_status=status, parsed_body=raw, transport_started=True)
    assert isinstance(outcome, SubmitOutcomeUnknown)
    assert outcome.reason == reason



def _nested(depth: int) -> Any:
    body: Any = 0
    for _ in range(depth):
        body = [body]
    return body


# Built inside the test: pytest would render a parametrized value this deep in its report.
_DEEP_BODIES: dict[str, Callable[[], Any]] = {
    "deep_body": lambda: _nested(1500),
    "deep_offer": lambda: [0, "on-req", None, None, _nested(1500), None, "SUCCESS", "ok"],
}


@pytest.mark.parametrize("status", [200, 401, 500])
@pytest.mark.parametrize("shape", _DEEP_BODIES)
def test_deeply_nested_submit_body_is_classified(status: int, shape: str) -> None:
    # json.loads accepts thousands of levels; the response digest's normaliser recursed
    # past the interpreter limit and raised RecursionError after transport.
    classify_submit_response(http_status=status, parsed_body=_DEEP_BODIES[shape](),
                             transport_started=True)
