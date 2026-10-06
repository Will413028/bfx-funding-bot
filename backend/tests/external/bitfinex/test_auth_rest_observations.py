"""Mocked HTTP coverage for the additive ledger observation read path."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingCredit,
    ActiveFundingOffer,
    BitfinexAuthREST,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.external.bitfinex.observations import (
    ObservationRequestBudget,
    ObservationRequestCapError,
    parse_offer_observations,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

KINDS = ("offer", "credit", "loan", "trade")


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default", credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("1000"),
    )


def _row(kind: str, row_id: int, mts: int) -> list[Any]:
    if kind == "offer":
        # the venue filters offers (like credits) by MTS_UPDATE; both stamps equal here
        return [row_id, "fUST", mts, mts, "NEGATIVE", "NEGATIVE", "LIMIT",
                None, None, 0, "CANCELED", None, None, None, "WIRE", 2, "extra"]
    if kind in ("credit", "loan"):
        return [row_id, "fUST", 1, mts - 1, mts, "WIRE", 0, "CLOSED (used)",
                "FIXED", None, None, "WIRE", 2, mts - 50, mts + 1, "extra"]
    return [row_id, "fUST", mts, 99, "NEGATIVE", "WIRE", 2, 1, "extra"]


def _response(rows: Any, value: str = "0.30000000000000004") -> httpx.Response:
    # Numeric literals in response text, never JSON strings or rounded floats.
    wire = json.dumps(rows).replace('"WIRE"', value).replace('"NEGATIVE"', f"-{value}")
    return httpx.Response(200, text=wire)


async def _fetch(rest: BitfinexAuthREST, kind: str, **kwargs: Any) -> Any:
    name = "fetch_trade_observations" if kind == "trade" else f"fetch_{kind}_history_observations"
    return await getattr(rest, name)(ctx=_ctx(), symbol="fUST", start_ms=100, end_ms=300, **kwargs)


def _id(row: Any, kind: str) -> str:
    if kind == "offer":
        return row.venue_offer_id
    if kind == "trade":
        return str(row.trade_id)
    return row.credit_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_short_page_includes_exact_start_and_preserves_requested_range(kind: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response([_row(kind, 1, 100), _row(kind, 2, 99), _row(kind, 3, 300)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind)
    assert [_id(row, kind) for row in result.rows] == ["1", "3"]
    assert result.complete and result.pages == 1
    assert (result.requested_start_ms, result.requested_end_ms) == (100, 300)
    endpoint = {"offer": "offers", "credit": "credits", "loan": "loans", "trade": "trades"}[kind]
    assert requests[0].url.path == f"/v2/auth/r/funding/{endpoint}/fUST/hist"
    assert json.loads(requests[0].content) == {"start": 100, "end": 300, "limit": 500, "sort": -1}
    assert requests[0].method == "POST"
    assert requests[0].headers["bfx-apikey"] == "KEY"
    assert "bfx-signature" in requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_same_ms_rows_split_across_pages_use_inclusive_cursor_and_id_dedup(kind: str) -> None:
    ends: list[int] = []
    pages = [
        [_row(kind, 1, 250), _row(kind, 2, 200)],
        [_row(kind, 2, 200), _row(kind, 3, 200)],
        [_row(kind, 3, 200), _row(kind, 4, 150)],
        [],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        end = json.loads(request.content)["end"]
        ends.append(end)
        assert end == [300, 200, 200, 150][len(ends) - 1]
        return _response(pages[len(ends) - 1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind, limit=2)
    assert result.complete and result.pages == 4
    assert {_id(row, kind) for row in result.rows} == {"1", "2", "3", "4"}
    assert len(result.rows) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_empty_page_proves_complete(kind: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response([]))) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind)
    assert result.complete and result.pages == 1 and result.rows == ()


@pytest.mark.asyncio
async def test_covered_pager_allows_symbol_none_for_account_offer_history() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _response([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(http=http)._page_covered_history(
            ctx=_ctx(), endpoint="v2/auth/r/funding/offers", symbol=None,
            parse=parse_offer_observations, mts=lambda row: row.mts_created,
            key=lambda row: row.venue_offer_id,
            start_ms=100, end_ms=300, limit=2, max_pages=5,
        )
    assert result.complete and paths == ["/v2/auth/r/funding/offers/hist"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("oldest", [99, 100])
async def test_full_page_reaching_start_is_incomplete(kind: str, oldest: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response([_row(kind, 1, 200), _row(kind, 2, oldest)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind, limit=2)
    assert not result.complete and result.pages == len(requests) == 1
    assert [_id(row, kind) for row in result.rows] == (["1"] if oldest < 100 else ["2", "1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_page_cap_is_incomplete(kind: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        i = len(requests)
        return _response([_row(kind, i * 2, 300 - i * 10), _row(kind, i * 2 + 1, 290 - i * 10)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind, limit=2, max_pages=2)
    assert not result.complete and result.pages == len(requests) == 2


@pytest.mark.asyncio
async def test_default_page_cap_is_five() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response([_row("trade", calls * 1000 + i, 300 - calls) for i in range(500)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), "trade")
    assert not result.complete and result.pages == calls == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_full_page_without_progress_is_incomplete(kind: str) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response([_row(kind, 1, 250), _row(kind, 2, 200)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind, limit=2)
    assert not result.complete and result.pages == calls == 2 and len(result.rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_short_page_rereading_only_the_boundary_ms_is_complete(kind: str) -> None:
    """A full page followed by a short page that re-reads only the inclusive
    boundary millisecond: the short page proves nothing older exists."""
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        rows = [_row(kind, 1, 250), _row(kind, 2, 200)] if calls == 1 else [_row(kind, 2, 200)]
        return _response(rows)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind, limit=2)
    assert result.complete and result.pages == calls == 2 and len(result.rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_rows_newer_than_end_are_filtered_without_invalidating_short_page(kind: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: _response([_row(kind, 1, 301), _row(kind, 2, 200)]),
    )) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind)
    assert result.complete and [_id(row, kind) for row in result.rows] == ["2"]


@pytest.mark.asyncio
async def test_filtering_or_dedup_does_not_turn_full_page_into_short_page() -> None:
    ends: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ends.append(json.loads(request.content)["end"])
        if len(ends) == 1:
            return _response([_row("trade", 1, 301), _row("trade", 1, 301)])
        return _response([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _fetch(BitfinexAuthREST(http=http), "trade", limit=2)
    assert result.complete and result.pages == 2 and result.rows == ()
    assert ends == [300, 300]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", ["0.30000000000000004", "0.300000000000000044408920985006261616945"])
async def test_history_decimals_and_raw_rows_preserve_all_wire_digits(kind: str, value: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: _response([_row(kind, 7, 200)], value),
    )) as http:
        result = await _fetch(BitfinexAuthREST(http=http), kind)
    [row] = result.rows
    assert row.amount == row.rate == Decimal(value)
    assert row.raw[-1] == "extra"
    assert not any(isinstance(value, float) for value in row.raw)
    if kind == "offer":
        assert row.amount_original == Decimal(value)
        assert row.status == "CANCELED" and row.raw[4] == Decimal(f"-{value}")
    if kind in ("credit", "loan"):
        assert row.credit_id == "7" and row.source_kind == kind
        assert row.mts_opening == 150 and row.mts_created == 199 and row.mts_updated == 200
        assert row.status == "CLOSED (used)" and row.raw[7] == row.status
    if kind == "trade":
        assert row.offer_id == 99 and row.maker is True and row.raw[4] == Decimal(f"-{value}")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["offer", "credit", "loan"])
@pytest.mark.parametrize("symbol", [None, "fUST"])
async def test_active_streams_are_decimal_raw_preserving_and_keep_unknown_status(
    kind: str, symbol: str | None,
) -> None:
    rows = [_row(kind, 7, 200)]
    rows[0][10 if kind == "offer" else 7] = "NEW VENUE STATUS"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(rows)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rest = BitfinexAuthREST(http=http)
        [row] = await getattr(rest, f"fetch_active_{kind}_observations")(ctx=_ctx(), symbol=symbol)
    assert row.amount == row.rate == Decimal("0.30000000000000004")
    assert row.status == "NEW VENUE STATUS" and row.raw[-1] == "extra"
    assert not any(isinstance(value, float) for value in row.raw)
    endpoint = {"offer": "offers", "credit": "credits", "loan": "loans"}[kind]
    assert requests[0].url.path == f"/v2/auth/r/funding/{endpoint}" + (f"/{symbol}" if symbol else "")
    assert requests[0].content == b"{}"
    if kind in ("credit", "loan"):
        assert row.credit_id == "7" and row.source_kind == kind and row.mts_opening == 150


@pytest.mark.asyncio
async def test_wallets_preserve_decimal_balances_raw_and_null_available() -> None:
    rows = [["funding", "UST", "WIRE", None, "WIRE", "extra"],
            ["exchange", "USD", "WIRE", None, None]]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response(rows))) as http:
        wallets = await BitfinexAuthREST(http=http).fetch_wallet_observations(ctx=_ctx())
    assert wallets[0].balance == wallets[0].available == Decimal("0.30000000000000004")
    assert (wallets[0].wallet_type, wallets[0].currency) == ("funding", "UST")
    assert wallets[0].raw[-1] == "extra" and wallets[0].raw[2] == wallets[0].balance
    assert wallets[1].wallet_type == "exchange" and wallets[1].available is None


@pytest.mark.asyncio
async def test_legacy_loan_prefix_and_optional_opening_remain_unchanged() -> None:
    row = _row("loan", 7, 200)
    row[13] = None
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response([row]))) as http:
        rest = BitfinexAuthREST(http=http)
        [legacy] = await rest.get_active_funding_loans(ctx=_ctx())
        assert legacy.credit_id == "loan:7" and legacy.mts_opening is None
        assert isinstance(legacy.rate, float)
        with pytest.raises(BitfinexShapeError):
            await rest.fetch_active_loan_observations(ctx=_ctx())


@pytest.mark.asyncio
async def test_legacy_offer_pager_cursor_remains_exclusive() -> None:
    ends: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ends.append(json.loads(request.content)["end"])
        if len(ends) == 1:
            return _response([_row("offer", 1, 250), _row("offer", 2, 200)])
        return _response([_row("offer", 3, 150)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        legacy = await BitfinexAuthREST(http=http).get_funding_offer_history(
            ctx=_ctx(), symbol="fUST", start_ms=100, end_ms=300, limit=2,
        )
    assert ends == [300, 199] and legacy.coverage.complete


@pytest.mark.asyncio
async def test_legacy_offer_history_keeps_newer_rows_and_marks_incomplete() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: _response([_row("offer", 1, 301)]),
    )) as http:
        rest = BitfinexAuthREST(http=http)
        legacy = await rest.get_funding_offer_history(
            ctx=_ctx(), symbol="fUST", start_ms=100, end_ms=300,
        )
        covered = await _fetch(rest, "offer")
    assert not legacy.coverage.complete and len(legacy.offers) == 1
    assert covered.complete and covered.rows == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["credit", "loan"])
@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("opening", ["absent", "null", "invalid"])
async def test_missing_mts_opening_raises(kind: str, active: bool, opening: str) -> None:
    row = _row(kind, 7, 200)
    if opening == "absent":
        row = row[:13]
    else:
        row[13] = None if opening == "null" else "invalid"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response([row]))) as http:
        rest = BitfinexAuthREST(http=http)
        with pytest.raises(BitfinexShapeError):
            if active:
                await getattr(rest, f"fetch_active_{kind}_observations")(ctx=_ctx())
            else:
                await _fetch(rest, kind)


@pytest.mark.asyncio
async def test_shared_request_cap_across_active_streams_symbols_and_history() -> None:
    paths: list[str] = []
    budget = ObservationRequestBudget(request_cap=3)

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/wallets"):
            return _response([])
        return _response([_row("trade", len(paths) * 2, 250), _row("trade", len(paths) * 2 + 1, 200)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rest = BitfinexAuthREST(http=http)
        await rest.fetch_wallet_observations(ctx=_ctx(), budget=budget)
        partial = await _fetch(rest, "trade", limit=2, budget=budget)
        assert not partial.complete and partial.pages == 2
        blocked = await rest.fetch_trade_observations(
            ctx=_ctx(), symbol="fUSD", start_ms=50, end_ms=400, budget=budget,
        )
        assert not blocked.complete and blocked.pages == 0 and blocked.rows == ()
        assert (blocked.requested_start_ms, blocked.requested_end_ms) == (50, 400)
        with pytest.raises(ObservationRequestCapError):
            await rest.fetch_active_offer_observations(ctx=_ctx(), budget=budget)
    assert len(paths) == budget.requests_used == 3


@pytest.mark.asyncio
async def test_request_cap_exhaustion_preserves_partial_rows() -> None:
    budget = ObservationRequestBudget(request_cap=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: _response([_row("trade", 1, 250), _row("trade", 2, 200)]),
    )) as http:
        result = await _fetch(BitfinexAuthREST(http=http), "trade", limit=2, budget=budget)
    assert not result.complete and result.pages == 1 and len(result.rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["wallet", "active_offer", "active_credit", "active_loan"])
@pytest.mark.parametrize("failure", ["http", "json", "shape"])
async def test_active_failure_raises_and_consumes_request_slot(stream: str, failure: str) -> None:
    budget = ObservationRequestBudget(request_cap=1)

    def handler(_: httpx.Request) -> httpx.Response:
        if failure == "http":
            return httpx.Response(503, text="unavailable")
        if failure == "json":
            return httpx.Response(200, text="invalid json")
        return _response({"not": "rows"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        rest = BitfinexAuthREST(http=http)
        with pytest.raises(BitfinexAPIError if failure == "http" else BitfinexShapeError):
            await getattr(rest, f"fetch_{stream}_observations")(ctx=_ctx(), budget=budget)
    assert budget.requests_used == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"start_ms": -1}, {"start_ms": 301}, {"limit": 0}, {"limit": 501}, {"max_pages": 0},
])
async def test_invalid_pager_arguments_make_no_requests(kwargs: dict[str, int]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid pager arguments must be rejected before HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError):
            await BitfinexAuthREST(http=http).fetch_trade_observations(
                ctx=_ctx(), symbol="fUST", **{"start_ms": 100, "end_ms": 300, **kwargs},
            )


def test_negative_request_cap_is_rejected() -> None:
    with pytest.raises(ValueError):
        ObservationRequestBudget(request_cap=-1)


def _offer(row_id: int, created: int, updated: int, symbol: str = "fUST") -> list[Any]:
    row = _row("offer", row_id, created)
    row[1], row[3] = symbol, updated
    return row


@pytest.mark.asyncio
async def test_offer_pager_keeps_rows_by_update_time_as_the_venue_filters() -> None:
    """Probed 2026-10-04: start/end filter MTS_UPDATE. An old offer ended in the window
    is kept; one created in the window but last updated after it is out of range."""
    rows = [_offer(1, created=10, updated=200), _offer(2, created=150, updated=150),
            _offer(3, created=150, updated=400), _offer(4, created=10, updated=50)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response(rows))) as http:
        result = await _fetch(BitfinexAuthREST(http=http), "offer")
    assert sorted(row.venue_offer_id for row in result.rows) == ["1", "2"]


@pytest.mark.asyncio
async def test_offer_by_id_request_shape_batches_and_filters_to_the_requested_ids() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        assert request.url.path == "/v2/auth/r/funding/offers/fUST/hist"
        # The venue answers for the ids it knows; an unrequested row must be ignored too.
        return _response([_offer(i, 1, 2) for i in (*bodies[-1]["id"][:-1], 999)])

    ids = [str(i) for i in range(1, 28)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lookup = await BitfinexAuthREST(http=http).fetch_offer_history_by_ids(
            ctx=_ctx(), symbol="fUST", offer_ids=ids, budget=ObservationRequestBudget(5))
    found = lookup.found
    assert lookup.failed == {}
    assert [(len(b["id"]), b["limit"]) for b in bodies] == [(25, 25), (2, 25)]
    assert all(isinstance(i, int) for b in bodies for i in b["id"])
    assert set(found) == set(ids[:24]) | {"26"}  # last id of each batch was not returned
    assert "999" not in found


@pytest.mark.asyncio
async def test_offer_by_id_budget_exhaustion_is_a_cause_per_id_not_an_exception() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response([]))) as http:
        lookup = await BitfinexAuthREST(http=http).fetch_offer_history_by_ids(
            ctx=_ctx(), symbol="fUST", offer_ids=[str(i) for i in range(1, 30)],
            budget=ObservationRequestBudget(1))
    assert lookup.found == {}  # the one batch asked came back empty
    assert lookup.failed == {str(i): "cap_exhausted" for i in range(26, 30)}


@pytest.mark.asyncio
async def test_offer_by_id_request_failure_marks_the_unasked_ids_and_stops() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lookup = await BitfinexAuthREST(http=http).fetch_offer_history_by_ids(
            ctx=_ctx(), symbol="fUST", offer_ids=[str(i) for i in range(1, 30)])
    assert len(calls) == 1  # no hammering a failing endpoint
    assert lookup.failed == {str(i): "request_failed" for i in range(1, 30)}


def test_active_wire_models_expose_normalized_observation_fields() -> None:
    offer = ActiveFundingOffer(
        venue_offer_id="1", symbol="fUST", amount=Decimal("2"), rate=0.0003, period_days=2,
        mts_created=1, status="ACTIVE", amount_original=Decimal("3"), mts_updated=2)
    credit = ActiveFundingCredit(
        credit_id="2", symbol="fUST", amount=Decimal("4"), rate=0.0004, period_days=2,
        status="ACTIVE", mts_created=3, mts_updated=4)
    assert offer.amount_original == Decimal("3")
    assert offer.mts_updated == 2
    assert credit.mts_created == 3
    assert credit.mts_updated == 4
