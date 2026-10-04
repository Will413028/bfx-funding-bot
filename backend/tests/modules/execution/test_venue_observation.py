"""Offline adapter contract tests: real additive REST under MockTransport."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.external.bitfinex.observations import (
    parse_credit_observations,
    parse_offer_observations,
    parse_wallet_observations,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.venue_observation import (
    HISTORY_INCOMPLETE_ALERT,
    OFFER_END_GRACE_MS,
    BitfinexVenueObservation,
    normalize_credit,
    normalize_credit_history,
    normalize_offer,
    normalize_offer_history,
    normalize_wallet,
)
from bfx_funding_bot.modules.ledger import LiveOffer, ObservationWindow, Scope, VenueObservation
from bfx_funding_bot.modules.observability import alerts

SCOPE = Scope(UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"), "test")
QUERY_START = 1_000_000
EMPTY_WINDOW = ObservationWindow(None, None, None)
ACTIVE_PATHS = ["wallets", "funding/offers", "funding/credits", "funding/loans"]
CTX = AccountContext(str(SCOPE.exchange_account_id), Credentials("TEST_KEY", "TEST_SECRET"),
                     Decimal("1000"))


def offer_row(status: str = "ACTIVE", *, symbol: str = "fUST", row_id: int = 10) -> list[Any]:
    return [row_id, symbol, 950_000, 999_900, "-4.30000000000000004",
            "-10.30000000000000004", "LIMIT", None, None, 64, status,
            None, None, None, "0.00030000000000000004", 2, "extra"]


def credit_row(status: str = "ACTIVE", *, symbol: str = "fUST", opening: int | None = 800_000,
               row_id: int = 20) -> list[Any]:
    return [row_id, symbol, 1, 990_000, 999_800, "-6.00000000000000004",
            0, status, "FIXED", None, None, "0.00030000000000000004", 2, opening,
            999_800, "extra"]


@pytest.mark.parametrize("status,expected", [
    ("ACTIVE", "active"), ("PARTIALLY FILLED", "partially_filled"),
    ("PARTIALLY FILLED @ 0.03", "partially_filled"),
])
def test_active_offer_status_table(status: str, expected: str) -> None:
    row = parse_offer_observations([offer_row(status)])[0]
    result = normalize_offer(row)
    assert result.status == expected
    assert result.amount_remaining == Decimal("4.30000000000000004")
    assert result.amount_original == Decimal("10.30000000000000004")
    assert result.rate == Decimal("0.00030000000000000004")
    assert result.offer_type == "LIMIT" and result.flags == 64 and result.rate_observed
    assert result.raw["row"][4] == "-4.30000000000000004"  # exact digits, as a string
    json.dumps(result.raw)  # JSON-safe: nothing for the ledger to convert
    assert result.mts_created == 950_000 and result.mts_updated == 999_900


@pytest.mark.parametrize("status,kind,prior", [
    ("EXECUTED", "executed", "active"),
    ("EXECUTED @ 0.03", "executed", "active"),
    ("EXECUTED (was: PARTIALLY FILLED @ 0.03)", "executed", "partially_filled"),
    ("CANCELED", "canceled", "active"),
    ("CANCELED @ 0.03", "canceled", "active"),
    ("CANCELED (was: PARTIALLY FILLED)", "canceled", "partially_filled"),
    ("EXPIRED", "canceled", "active"),
])
def test_offer_history_status_table(status: str, kind: str, prior: str) -> None:
    result = normalize_offer_history(parse_offer_observations([offer_row(status)])[0])
    assert result.terminal_kind == kind and result.offer.status == prior
    assert result.occurred_at_ms == 999_900  # MTS_UPDATE, not MTS_CREATE


@pytest.mark.parametrize("source_kind", ["credit", "loan"])
@pytest.mark.parametrize("status", [
    "CLOSED (used)", "CLOSED (expired)", "CLOSED (reduced)",
    # Seen in production funding_credit_history (2026-10-01 replay).
    "CLOSED (no more position)", "CLOSED",
])
def test_credit_history_closed_status_table(status: str, source_kind: Any) -> None:
    row = parse_credit_observations([credit_row(status)], source_kind=source_kind)[0]
    result = normalize_credit_history(row)
    assert result.terminal_kind == "closed" and result.credit.status == "active"
    assert result.credit.source_kind == source_kind and result.credit.venue_credit_id == "20"
    assert result.credit.mts_opening == 800_000 and result.occurred_at_ms == 999_800
    assert result.credit.amount == Decimal("6.00000000000000004")
    json.dumps(result.credit.raw)
    assert result.credit.raw["row"][5] == "-6.00000000000000004"


@pytest.mark.parametrize("source_kind", ["credit", "loan"])
def test_active_credit_identity_and_required_opening(source_kind: Any) -> None:
    row = parse_credit_observations([credit_row()], source_kind=source_kind)[0]
    result = normalize_credit(row)
    assert result.status == "active" and result.source_kind == source_kind
    assert result.venue_credit_id == "20"  # never loan:20
    assert result.mts_opening == 800_000 and result.mts_created == 990_000
    with pytest.raises(ValueError, match="MTS_OPENING"):
        normalize_credit(replace(row, mts_opening=None))  # type: ignore[arg-type]


def test_wallet_symbol_and_null_available_are_explicit() -> None:
    funding, exchange = parse_wallet_observations([
        ["funding", "UST", "100.00000000000000004", 0, "4.00000000000000004"],
        ["exchange", "ETH", "10", 0, "10"],
    ])
    assert normalize_wallet(funding).symbol == "fUST"
    assert normalize_wallet(funding).available == Decimal("4.00000000000000004")
    assert normalize_wallet(exchange).symbol is None
    with pytest.raises(ValueError, match="unobserved"):
        normalize_wallet(replace(funding, available=None))


class Venue:
    """A deterministic HTTP venue, with local time advancing on each read."""

    def __init__(self) -> None:
        self.now = QUERY_START
        self.paths: list[str] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.active: dict[str, Any] = {
            "wallets": [["funding", "UST", "100", 0, "90"]],
            "funding/offers": [offer_row()],
            "funding/credits": [credit_row()],
            "funding/loans": [],
        }
        self.history: dict[str, Any] = {
            "offers": [offer_row("EXECUTED (was: PARTIALLY FILLED)")],
            "credits": [credit_row("CLOSED (used)")], "loans": [],
            "trades": [[30, "fUST", 800_000, 10, "-6.00000000000000004",
                        "0.00030000000000000004", 2, 1]],
        }
        self.override: Callable[[str, dict[str, Any]], httpx.Response | None] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.now += 100
        path = request.url.path.removeprefix("/v2/auth/r/")
        body = json.loads(request.content)
        self.paths.append(path)
        self.requests.append((path, body))
        if self.override is not None:
            response = self.override(path, body)
            if response is not None:
                return response
        if path.endswith("/hist"):
            kind = path.split("/")[1]
            rows = self.history[kind]
            if kind == "offers" and "id" in body:  # by id (probed 2026-10-04): no time filter
                rows = [row for row in rows if row[0] in body["id"]]
            elif kind in ("offers", "credits"):  # the venue filters on MTS_UPDATE
                stamp = {"offers": 3, "credits": 4}[kind]
                rows = [row for row in rows if body["start"] <= row[stamp] <= body["end"]]
            # Use the requested symbol; four independent fixtures need not
            # duplicate the same shape. Venue timestamps remain unchanged.
            rows = [[row[0], path.split("/")[2], *row[2:]] for row in rows]
        else:
            rows = self.active[path]
        return httpx.Response(200, json=rows)


async def observe(venue: Venue, *, window: ObservationWindow = EMPTY_WINDOW,
                  request_cap: int = 48) -> Any:
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        port: VenueObservation = BitfinexVenueObservation(
            rest=BitfinexAuthREST(http=http), ctx=CTX, scope=SCOPE,
            clock_ms=lambda: venue.now, request_cap=request_cap)
        return await port.observe(SCOPE, QUERY_START, window)


async def test_request_order_confirmation_is_active_only_and_end_after_active() -> None:
    venue = Venue()
    first, confirmation, confirmation_start = await observe(venue)
    history_paths = [f"funding/{kind}/fUST/hist" for kind in ("offers", "credits", "loans", "trades")]
    assert venue.paths == ACTIVE_PATHS + history_paths + ACTIVE_PATHS
    assert first.coverage.complete and confirmation.coverage.active_complete
    assert not confirmation.offer_history and not confirmation.credit_history and not confirmation.trades
    assert first.finished_at_ms == confirmation_start == QUERY_START + 800
    assert confirmation.finished_at_ms == QUERY_START + 1200
    assert first.coverage.history_requested_end_ms == QUERY_START + 400
    assert all(body["end"] == QUERY_START + 400 for _, body in venue.requests if "end" in body)
    assert first.coverage.offer_history_pages == 1 and first.coverage.credit_history_pages == 2
    assert first.coverage.wallet_pages == first.coverage.offer_pages == 1
    assert first.coverage.history_oldest_mts_created == 950_000
    assert first.coverage.history_newest_mts_created == 990_000
    assert first.trades[0].venue_offer_id == "10" and first.trades[0].maker is True
    assert first.trades[0].amount == Decimal("6.00000000000000004")


@pytest.mark.parametrize("has_credits", [False, True])
async def test_no_anchor_uses_query_start_minus_margin(has_credits: bool) -> None:
    venue = Venue()
    if not has_credits:
        venue.active["funding/credits"] = []
    first, _, _ = await observe(venue)
    assert first.coverage.history_requested_start_ms == 940_000
    assert [body["start"] for path, body in venue.requests
            if path.endswith("/hist") and "trades" not in path] == [940_000] * 3


async def test_window_shared_range_and_trades_cover_oldest_credit_or_loan_opening() -> None:
    """No accepted query yet: every current opening needs trades evidence."""
    venue = Venue()
    venue.active["funding/loans"] = [credit_row(symbol="fUST", opening=700_000, row_id=21)]
    first, _, _ = await observe(venue, window=ObservationWindow(980_000, None, 920_000))
    assert first.coverage.history_requested_start_ms == 920_000
    assert first.coverage.trades_requested_start_ms == 640_000
    assert first.coverage.trades_requested_end_ms == first.coverage.history_requested_end_ms
    for path, body in venue.requests:
        if path.endswith("/hist"):
            assert body == {"start": 640_000 if "trades" in path else 920_000,
                            "end": QUERY_START + 400, "limit": 500, "sort": -1}


async def test_old_credits_do_not_widen_trades_after_an_accepted_query() -> None:
    """Openings before the previous accepted query were in that basis and are
    carried; trades need not reach back to them on every observation."""
    venue = Venue()
    venue.active["funding/loans"] = [credit_row(symbol="fUST", opening=700_000, row_id=21)]
    first, _, _ = await observe(venue, window=ObservationWindow(980_000, 970_000, 910_000))
    assert first.coverage.trades_requested_start_ms == 910_000
    assert first.coverage.history_requested_start_ms == 910_000


async def test_trade_range_includes_history_when_all_openings_are_newer() -> None:
    venue = Venue()
    venue.active["funding/credits"] = [credit_row(opening=990_000)]
    first, _, _ = await observe(venue, window=ObservationWindow(800_000, None, 740_000))
    assert first.coverage.trades_requested_start_ms == 740_000


async def test_symbol_union_wallets_active_credits_and_loans() -> None:
    venue = Venue()
    venue.active["wallets"].extend([
        ["funding", "USD", "0", 0, "0"], ["exchange", "EUR", "10", 0, "10"]])
    venue.active["funding/offers"] = [offer_row(symbol="fEUR")]
    venue.active["funding/credits"] = [credit_row(symbol="fETH")]
    venue.active["funding/loans"] = [credit_row(symbol="fBTC", row_id=21)]
    first, _, _ = await observe(venue)
    assert venue.paths == ACTIVE_PATHS + [
        f"funding/{kind}/{symbol}/hist" for symbol in ("fBTC", "fETH", "fUSD", "fUST")
        for kind in ("offers", "credits", "loans", "trades")] + ACTIVE_PATHS
    assert first.coverage.complete
    assert first.coverage.offer_history_pages == 4 and first.coverage.credit_history_pages == 8
    assert first.coverage.history_symbols == frozenset({"fBTC", "fETH", "fUSD", "fUST"})


async def test_anchor_attempt_symbols_are_fetched_and_declared() -> None:
    """G1: an UNKNOWN on a symbol no wallet or credit names still gets its history fetched."""
    venue = Venue()
    window = ObservationWindow(900_000, None, 840_000, frozenset({"fEUR"}))
    first, _, _ = await observe(venue, window=window)
    fetched = {path.split("/")[1:3][1] for path in venue.paths if path.endswith("/hist")}
    assert fetched == {"fEUR", "fUST"}
    assert first.coverage.history_symbols == frozenset({"fEUR", "fUST"})
    assert first.coverage.complete


@pytest.mark.parametrize("stream", ["offers", "credits", "loans"])
@pytest.mark.parametrize("unknown", ["NEW VENUE STATUS", "ACTIVEISH"])
async def test_unknown_active_status_raises_without_observation(stream: str, unknown: str) -> None:
    venue = Venue()
    venue.active[f"funding/{stream}"] = [offer_row(unknown) if stream == "offers" else credit_row(unknown)]
    with pytest.raises(ValueError, match="unknown active"):
        await observe(venue)
    assert all(not path.endswith("/hist") for path in venue.paths)


@pytest.mark.parametrize("stream", ["offers", "credits", "loans"])
async def test_unknown_history_status_incomplete_and_alert(stream: str, monkeypatch: Any) -> None:
    venue = Venue()
    venue.history[stream] = [offer_row("NEW VENUE STATUS") if stream == "offers" else credit_row("NEW VENUE STATUS")]
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: emitted.append((event, fields)))
    first, confirmation, _ = await observe(venue)
    assert not first.coverage.complete and confirmation.coverage.active_complete
    assert first.coverage.offer_history_complete is (stream != "offers")
    assert first.coverage.credit_history_complete is (stream == "offers")
    assert first.coverage.trades_complete
    assert len(emitted) == 1 and emitted[0][0] == HISTORY_INCOMPLETE_ALERT
    assert emitted[0][1]["stream"] == stream and emitted[0][1]["level"] == alerts.WARNING
    assert "NEW VENUE STATUS" in emitted[0][1]["reason"]


@pytest.mark.parametrize("stream", ACTIVE_PATHS)
@pytest.mark.parametrize("confirmation", [False, True])
async def test_active_failure_raises_even_in_confirmation(stream: str, confirmation: bool) -> None:
    venue = Venue()
    def fail(path: str, body: dict[str, Any]) -> httpx.Response | None:
        if path == stream and venue.paths.count(stream) == (2 if confirmation else 1):
            return httpx.Response(500, json=["error", 500, "offline failure"])
        return None
    venue.override = fail
    with pytest.raises(BitfinexAPIError):
        await observe(venue)


@pytest.mark.parametrize("stream", ["offers", "credits", "loans", "trades"])
async def test_history_failure_only_marks_affected_coverage_and_alerts(stream: str, monkeypatch: Any) -> None:
    venue = Venue()
    venue.override = lambda path, _: (httpx.Response(500, json=["error", 500, "offline failure"])
                                     if path == f"funding/{stream}/fUST/hist" else None)
    emitted: list[str] = []
    monkeypatch.setattr(alerts, "emit", lambda _, **fields: emitted.append(fields["stream"]))
    first, confirmation, _ = await observe(venue)
    assert not first.coverage.complete and confirmation.coverage.active_complete
    assert first.coverage.offer_history_complete is (stream != "offers")
    assert first.coverage.credit_history_complete is (stream not in ("credits", "loans"))
    assert first.coverage.trades_complete is (stream != "trades")
    assert first.coverage.offer_history_pages == 1 and first.coverage.credit_history_pages == 2
    assert emitted == [stream]


@pytest.mark.parametrize("stream", ["credits", "loans"])
async def test_missing_opening_never_falls_back_to_create(stream: str) -> None:
    venue = Venue()
    venue.active[f"funding/{stream}"] = [credit_row(opening=None)]
    with pytest.raises(BitfinexShapeError):
        await observe(venue)
    assert not any(path.endswith("/hist") for path in venue.paths)


async def test_shared_budget_exhaustion_is_incomplete_and_reserves_confirmation(monkeypatch: Any) -> None:
    venue = Venue()
    emitted: list[str] = []
    monkeypatch.setattr(alerts, "emit", lambda _, **fields: emitted.append(fields["stream"]))
    # Two history slots only: offer + credit use them; loan/trade are incomplete.
    first, confirmation, _ = await observe(venue, request_cap=10)
    assert venue.paths == [*ACTIVE_PATHS, "funding/offers/fUST/hist", "funding/credits/fUST/hist", *ACTIVE_PATHS]
    assert first.coverage.offer_history_complete
    assert not first.coverage.credit_history_complete and not first.coverage.trades_complete
    assert first.coverage.credit_history_pages == 1
    assert confirmation.coverage.active_complete and emitted == ["loans", "trades"]
    assert first.coverage.history_requested_start_ms == 940_000


async def test_budget_is_shared_across_symbols(monkeypatch: Any) -> None:
    venue = Venue()
    venue.active["wallets"].append(["funding", "USD", "0", 0, "0"])
    emitted: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "emit", lambda _, **fields: emitted.append((fields["symbol"], fields["stream"])))
    # Four slots finish fUSD; fUST must not get a fresh budget.
    first, confirmation, _ = await observe(venue, request_cap=12)
    assert venue.paths == ACTIVE_PATHS + [f"funding/{kind}/fUSD/hist" for kind in
                                        ("offers", "credits", "loans", "trades")] + ACTIVE_PATHS
    assert not first.coverage.offer_history_complete
    assert not first.coverage.credit_history_complete and not first.coverage.trades_complete
    assert confirmation.coverage.active_complete
    assert emitted == [("fUST", kind) for kind in ("offers", "credits", "loans", "trades")]


async def test_five_page_cap_incomplete_even_with_request_budget_remaining(monkeypatch: Any) -> None:
    venue = Venue()
    emitted: list[str] = []
    monkeypatch.setattr(alerts, "emit", lambda _, **fields: emitted.append(fields["stream"]))
    def full_pages(path: str, body: dict[str, Any]) -> httpx.Response | None:
        if path == "funding/offers/fUST/hist":
            page = venue.paths.count(path)
            rows = [offer_row("CANCELED", row_id=page * 1000 + i) for i in range(500)]
            for row in rows:
                row[2] = 980_000 - page * 1000
            return httpx.Response(200, json=rows)
        return None
    venue.override = full_pages
    first, confirmation, _ = await observe(venue)
    assert first.coverage.offer_history_pages == 5
    assert not first.coverage.offer_history_complete and confirmation.coverage.active_complete
    assert first.coverage.credit_history_complete and first.coverage.trades_complete
    assert emitted == ["offers"]


async def test_scope_mismatch_rejected_before_http() -> None:
    venue = Venue()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        with pytest.raises(ValueError, match="context account"):
            BitfinexVenueObservation(rest=BitfinexAuthREST(http=http), ctx=replace(CTX, account_id="other"),
                                     scope=SCOPE)
        port = BitfinexVenueObservation(rest=BitfinexAuthREST(http=http), ctx=CTX, scope=SCOPE)
        with pytest.raises(ValueError, match="scope mismatch"):
            await port.observe(replace(SCOPE, deployment_environment="other"), QUERY_START, EMPTY_WINDOW)
    assert venue.paths == []


async def test_only_funding_wallets_enter_the_observation() -> None:
    """An exchange wallet with a venue-null balance must not fail the observation,
    and non-funding churn must not enter the active digest."""
    venue = Venue()
    venue.active["wallets"].append(["exchange", "ETH", "10", 0, None])
    first, confirmation, _ = await observe(venue)
    assert {w.wallet_type for w in first.wallets} == {"funding"}
    assert {w.wallet_type for w in confirmation.wallets} == {"funding"}


@pytest.mark.parametrize("status", ["CLOSEDISH", "ACTIVE", "EXPIRED"])
def test_credit_history_rejects_non_closed_status(status: str) -> None:
    row = parse_credit_observations([credit_row(status)], source_kind="credit")[0]
    with pytest.raises(ValueError, match="history status"):
        normalize_credit_history(row)



# --- F2: offers that vanished from the active list are looked up by id -------------------

def _live(*ids: int, symbol: str = "fUST") -> tuple[LiveOffer, ...]:
    return tuple(LiveOffer(str(i), symbol) for i in ids)


def _vanish_window(*ids: int, symbol: str = "fUST") -> ObservationWindow:
    return ObservationWindow(None, 970_000, 910_000, frozenset(), _live(*ids, symbol=symbol))


def _by_id_requests(venue: Venue) -> list[dict[str, Any]]:
    return [body for path, body in venue.requests if path.endswith("/hist") and "id" in body]


def _ended(row_id: int, status: str = "CANCELED", *, created: int = 100, updated: int = 999_000,
           symbol: str = "fUST") -> list[Any]:
    row = offer_row(status, symbol=symbol, row_id=row_id)
    row[2], row[3] = created, updated
    return row


async def test_a_vanished_old_offer_is_fetched_by_id_without_widening_the_window() -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.history["offers"] = [_ended(77, updated=500)]  # ended long before any window
    first, _, _ = await observe(venue, window=_vanish_window(77))
    assert _by_id_requests(venue) == [{"id": [77], "limit": 25}]
    assert [item.offer.venue_offer_id for item in first.offer_history] == ["77"]
    assert first.offer_history[0].terminal_kind == "canceled"
    assert first.coverage.offer_history_complete
    # R-e and R6 keep their range: no widening, shared by every history stream.
    assert first.coverage.history_requested_start_ms == 910_000
    windowed = [body for path, body in venue.requests
                if path.endswith("/hist") and "id" not in body and "trades" not in path]
    assert windowed and all(body["start"] == 910_000 for body in windowed)


async def test_a_vanished_offer_also_seen_in_the_window_appears_once() -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.history["offers"] = [_ended(77, updated=999_900)]  # in window too
    first, _, _ = await observe(venue, window=_vanish_window(77))
    assert [item.offer.venue_offer_id for item in first.offer_history] == ["77"]


async def test_a_still_active_offer_is_not_looked_up() -> None:
    venue = Venue()  # offer 10 is active
    first, _, _ = await observe(venue, window=_vanish_window(10))
    assert _by_id_requests(venue) == [] and first.coverage.complete


async def test_a_vanished_id_the_venue_does_not_return_is_incomplete(monkeypatch: Any) -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.history["offers"] = [_ended(77)]
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts, "emit", lambda _, **fields: emitted.append(fields))
    first, _, _ = await observe(venue, window=_vanish_window(77, 78))
    assert [item.offer.venue_offer_id for item in first.offer_history] == ["77"]
    assert not first.coverage.offer_history_complete and not first.coverage.complete
    assert first.coverage.credit_history_complete  # per stream, not a blanket flag
    assert [(f["stream"], f["reason"]) for f in emitted] == [
        ("offers_by_id", "not_returned")]


async def test_a_vanished_id_returned_non_terminal_is_incomplete() -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.history["offers"] = [_ended(77, "ACTIVE")]
    first, _, _ = await observe(venue, window=_vanish_window(77))
    assert first.offer_history == () and not first.coverage.offer_history_complete


async def test_by_id_failure_is_incomplete_not_an_error() -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.override = lambda _, body: httpx.Response(500, text="boom") if "id" in body else None
    first, confirmation, _ = await observe(venue, window=_vanish_window(77))
    assert not first.coverage.offer_history_complete and confirmation.coverage.active_complete


async def test_vanished_ids_are_batched_per_symbol_and_counted() -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    ids = list(range(1000, 1030))
    venue.history["offers"] = [_ended(i, updated=500) for i in ids]
    window = ObservationWindow(None, 970_000, 910_000, frozenset(),
                               (*_live(*ids), LiveOffer("9", "fUSD")))
    first, _, _ = await observe(venue, window=window)
    assert sorted(len(body["id"]) for body in _by_id_requests(venue)) == [1, 5, 25]
    assert {path for path, body in venue.requests if "id" in body} == {
        "funding/offers/fUST/hist", "funding/offers/fUSD/hist"}
    assert not first.coverage.offer_history_complete  # fUSD offer 9 is not in the fixture rows
    assert len([i for i in first.offer_history if i.offer.symbol == "fUST"]) == 30
    assert first.coverage.history_symbols == frozenset({"fUST", "fUSD"})  # G1: declared


async def test_by_id_requests_draw_on_the_shared_budget(monkeypatch: Any) -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.history["offers"] = [_ended(77, updated=500)]
    monkeypatch.setattr(alerts, "emit", lambda *_, **__: None)
    # 4 active + offers window + one by-id = 6 slots; credits, loans, trades starve.
    first, confirmation, _ = await observe(venue, window=_vanish_window(77), request_cap=10)
    assert len(_by_id_requests(venue)) == 1 and first.coverage.offer_history_complete
    assert not first.coverage.credit_history_complete and confirmation.coverage.active_complete


# --- C+E: grace for an id the venue does not return, then an unconfirmed end ------------------

async def _cycles(venue: Venue, windows: list[ObservationWindow], *, gaps: list[int],
                  restart_before: frozenset[int] = frozenset(), request_cap: int = 48) -> list[Any]:
    """Run observations on one port (a process), advancing the clock by ``gaps`` between them."""
    out: list[Any] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        def make() -> BitfinexVenueObservation:
            return BitfinexVenueObservation(
                rest=BitfinexAuthREST(http=http), ctx=CTX, scope=SCOPE, clock_ms=lambda: venue.now,
                request_cap=request_cap)

        port = make()
        for i, window in enumerate(windows):
            if i in restart_before:
                port = make()
            venue.now += gaps[i]
            out.append((await port.observe(SCOPE, venue.now, window))[0])
    return out


def _missing_venue() -> Venue:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.history["offers"] = []  # the venue does not know offer 77
    return venue


async def test_within_the_grace_a_missing_id_stays_incomplete_then_is_unconfirmed(monkeypatch: Any) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: emitted.append((event, fields)))
    venue = _missing_venue()
    window = _vanish_window(77)
    first, second, third = await _cycles(
        venue, [window] * 3, gaps=[0, OFFER_END_GRACE_MS - 5_000, 10_000])
    assert not first.coverage.offer_history_complete and first.unconfirmed_ends == ()
    assert not second.coverage.offer_history_complete and second.unconfirmed_ends == ()  # not yet 120 s
    assert third.coverage.offer_history_complete and third.coverage.complete
    assert third.unconfirmed_ends == ("77",) and third.offer_history == ()  # nothing made up
    ours = [f for event, f in emitted if event == alerts.OFFER_END_UNCONFIRMED]
    assert len(ours) == 1 and ours[0]["venue_offer_id"] == "77" and ours[0]["symbol"] == "fUST"


async def test_by_id_is_retried_each_cycle_while_the_grace_runs() -> None:
    venue = _missing_venue()
    first, second = await _cycles(venue, [_vanish_window(77)] * 2, gaps=[0, 30_000])
    assert not first.coverage.offer_history_complete and not second.coverage.offer_history_complete
    assert len(_by_id_requests(venue)) == 2


async def test_a_restart_starts_a_new_grace() -> None:
    venue = _missing_venue()
    window = _vanish_window(77)
    _, second, third = await _cycles(
        venue, [window] * 3, gaps=[0, OFFER_END_GRACE_MS + 10_000, OFFER_END_GRACE_MS + 10_000],
        restart_before=frozenset({1}))
    assert not second.coverage.offer_history_complete and second.unconfirmed_ends == ()
    assert third.unconfirmed_ends == ("77",)  # the restarted process, past its own grace


async def test_an_id_that_shows_up_resets_its_grace_and_is_not_unconfirmed() -> None:
    venue = _missing_venue()
    window = _vanish_window(77)
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        port = BitfinexVenueObservation(
            rest=BitfinexAuthREST(http=http), ctx=CTX, scope=SCOPE, clock_ms=lambda: venue.now)
        first = (await port.observe(SCOPE, venue.now, window))[0]
        venue.history["offers"] = [_ended(77)]  # the lag is over
        venue.now += 30_000
        second = (await port.observe(SCOPE, venue.now, window))[0]
        venue.history["offers"] = []
        venue.now += OFFER_END_GRACE_MS + 10_000  # long after the first sighting
        third = (await port.observe(SCOPE, venue.now, window))[0]
    assert not first.coverage.offer_history_complete
    assert second.coverage.offer_history_complete and second.unconfirmed_ends == ()
    assert [i.offer.venue_offer_id for i in second.offer_history] == ["77"]
    assert not third.coverage.offer_history_complete  # a new sighting, a new grace


CAUSES = ("not_returned", "request_failed", "cap_exhausted", "unparseable")


def _venue_with_cause(cause: str) -> tuple[Venue, int]:
    """A venue whose by-id answer for offer 77 has the given cause; the request cap to use."""
    venue = _missing_venue()
    cap = 48
    if cause == "request_failed":
        venue.override = lambda _, body: httpx.Response(500, text="boom") if "id" in body else None
    elif cause == "cap_exhausted":
        cap = 9  # 4 active reads + the windowed offers page leave nothing for the lookup
    elif cause == "unparseable":
        venue.history["offers"] = [_ended(77, "ACTIVE", updated=100)]  # non-terminal, outside the window
    return venue, cap


@pytest.mark.parametrize("cause", CAUSES)
async def test_every_cause_takes_the_same_grace_then_unconfirmed_path(cause: str, monkeypatch: Any) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: emitted.append((event, fields)))
    venue, cap = _venue_with_cause(cause)
    window = _vanish_window(77)
    first, second, third = await _cycles(
        venue, [window] * 3, gaps=[0, OFFER_END_GRACE_MS - 5_000, 10_000], request_cap=cap)
    for within in (first, second):  # inside the grace: incomplete, nothing declared
        assert not within.coverage.offer_history_complete and within.unconfirmed_ends == ()
    assert third.coverage.offer_history_complete and third.unconfirmed_ends == ("77",)
    assert third.offer_history == ()  # nothing made up
    unconfirmed = [f for event, f in emitted if event == alerts.OFFER_END_UNCONFIRMED]
    assert [(f["venue_offer_id"], f["cause"]) for f in unconfirmed] == [("77", cause)]
    incomplete = [f["reason"] for event, f in emitted
                  if event == HISTORY_INCOMPLETE_ALERT and f["stream"] == "offers_by_id"]
    assert incomplete == [cause, cause]  # one per cycle inside the grace


async def test_by_id_rows_are_their_own_evidence_not_window_coverage() -> None:
    venue = Venue()
    venue.active["funding/offers"] = []
    venue.history["offers"] = [_ended(77, created=100, updated=500)]  # outside the window
    first, _, _ = await observe(venue, window=_vanish_window(77))
    assert [i.offer.venue_offer_id for i in first.offer_history] == ["77"]  # terminal evidence
    cov = first.coverage
    assert cov.offer_history_pages == 1  # the windowed query only, not the lookup request
    assert (cov.history_requested_start_ms, cov.history_oldest_mts_created) == (910_000, 990_000)
    assert cov.history_oldest_mts_created != 100 and cov.history_newest_mts_created == 990_000


def test_the_adapter_output_is_json_safe_for_every_decimal() -> None:
    wire = offer_row()
    wire[4], wire[5], wire[14] = (Decimal(wire[4]), Decimal(wire[5]), Decimal(wire[14]))
    row = parse_offer_observations([wire])[0]
    assert any(isinstance(v, Decimal) for v in row.raw)  # the REST client decodes exactly
    offer = normalize_offer(row)
    assert json.loads(json.dumps(offer.raw)) == offer.raw
    assert offer.raw["row"][14] == "0.00030000000000000004"
    nested = replace(row, raw=(*row.raw[:9], {"flag": Decimal("1.50")}, *row.raw[10:]))
    assert normalize_offer(nested).raw["row"][9] == {"flag": "1.50"}
