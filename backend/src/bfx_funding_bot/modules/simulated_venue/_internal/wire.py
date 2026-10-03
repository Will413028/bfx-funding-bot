"""Bitfinex wire shapes, written from the public docs, not from the client's parsers.

Funding offer / credit / loan / trade / wallet / ledger rows are positional arrays.
A write answers a notification `[MTS, TYPE, MESSAGE_ID, null, DATA, CODE, STATUS,
TEXT]`: TEXT sits at index 7 (docs.bitfinex.com, submit and cancel funding offer).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from bfx_funding_bot.modules.simulated_venue._internal.state import (
    LedgerEntry,
    Lending,
    Offer,
    Trade,
    VenueState,
    Wallet,
)
from bfx_funding_bot.modules.simulated_venue.contracts import SimulatedVenueConfig

DEFAULT_PAGE_LIMIT = 25  # Bitfinex's default when `limit` is omitted
LEDGER_INTEREST_DESCRIPTION = "Margin Funding Payment on wallet funding"


def dumps(value: Any) -> bytes:
    """JSON with Decimals as numbers (amounts keep at most 8 decimals, so exact)."""
    def default(obj: object) -> float:
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError(f"not JSON serializable: {type(obj).__name__}")

    return json.dumps(value, default=default, separators=(",", ":")).encode()


def _rate_text(rate: Decimal) -> str:
    return format(rate, "f")


def offer_status(offer: Offer) -> str:
    at = f"@ {_rate_text(offer.rate)}"
    prior = f"(was: PARTIALLY FILLED {at})"
    if offer.status == "ACTIVE":
        return "ACTIVE"
    if offer.status == "PARTIAL":
        return f"PARTIALLY FILLED {at}"
    if offer.status == "EXECUTED":
        return f"EXECUTED {prior}" if offer.was_partial else f"EXECUTED {at}"
    return f"CANCELED {prior}" if offer.was_partial else "CANCELED"


def offer_row(offer: Offer) -> list[Any]:
    """21 fields; AMOUNT is the remaining size, AMOUNT_ORIG the submitted size."""
    return [
        offer.offer_id, offer.symbol, offer.mts_create, offer.mts_update,
        offer.remaining, offer.amount_original, "LIMIT", None, None, 0,
        offer_status(offer), None, None, None, offer.rate, offer.period,
        0, 0, None, 0, None,
    ]


def lending_row(lending: Lending) -> list[Any]:
    """Credit and loan share one 22-field layout; SIDE 1 = we are the lender."""
    last_payout = lending.close_mts if lending.close_mts is not None else lending.paid_through
    return [
        lending.lending_id, lending.symbol, 1, lending.mts_create, lending.mts_update,
        lending.amount, 0, lending.status, "FIXED", None, None, lending.rate,
        lending.period, lending.opening, last_payout, 0, 0, None, 0, None, 0, None,
    ]


def trade_row(trade: Trade) -> list[Any]:
    return [
        trade.trade_id, trade.symbol, trade.mts, trade.offer_id, trade.amount,
        trade.rate, trade.period, 1,
    ]


def ledger_row(entry: LedgerEntry) -> list[Any]:
    return [
        entry.ledger_id, entry.currency, None, entry.mts, None, entry.amount,
        entry.balance, None, LEDGER_INTEREST_DESCRIPTION,
    ]


def wallet_row(currency: str, wallet: Wallet, available: Decimal) -> list[Any]:
    return ["funding", currency, wallet.balance, 0, available, None, None]


def notification(
    mts: int, type_: str, data: Any, status: str, text: str, code: int | None = None,
) -> list[Any]:
    return [mts, type_, None, None, data, code, status, text]


def error_body(code: int, message: str) -> list[Any]:
    """The venue's own refusal shape, answered with an HTTP 5xx."""
    return ["error", code, message]


@dataclass(frozen=True, slots=True)
class PageRequest:
    start: int | None
    end: int | None
    limit: int
    ascending: bool


def parse_page_request(body: Mapping[str, Any], *, max_limit: int) -> PageRequest:
    def _int(name: str) -> int | None:
        value = body.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        return value

    limit = _int("limit")
    sort = _int("sort")
    if sort not in (None, 1, -1):
        raise ValueError("sort must be 1 or -1")
    return PageRequest(
        start=_int("start"), end=_int("end"),
        limit=min(DEFAULT_PAGE_LIMIT if limit is None else max(limit, 0), max_limit),
        ascending=sort == 1,
    )


def page[T](
    rows: Sequence[T], *, request: PageRequest, ts: Callable[[T], int],
    ident: Callable[[T], int],
) -> list[T]:
    """Rows with `start <= ts <= end`, ordered by (ts, id), exactly `limit` while any remain.

    Descending (the default and what the covered pager sends) is newest first with
    the larger id first inside one millisecond; `end` is inclusive, so a client may
    re-read its boundary millisecond.
    """
    selected = [
        r for r in rows
        if (request.start is None or ts(r) >= request.start)
        and (request.end is None or ts(r) <= request.end)
    ]
    selected.sort(key=lambda r: (ts(r), ident(r)), reverse=not request.ascending)
    return selected[: request.limit]


Route = tuple[Literal["wallets", "active", "hist", "ledger", "submit", "cancel", "cancel_all"],
              dict[str, str]]

_SYMBOL = r"f[A-Z0-9]{2,10}"
_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), name) for pattern, name in (
        (r"^v2/auth/r/wallets$", "wallets"),
        (rf"^v2/auth/r/funding/(?P<stream>offers|credits|loans)(?:/(?P<symbol>{_SYMBOL}))?$",
         "active"),
        (rf"^v2/auth/r/funding/(?P<stream>offers|credits|loans|trades)"
         rf"(?:/(?P<symbol>{_SYMBOL}))?/hist$", "hist"),
        (r"^v2/auth/r/ledgers/(?P<currency>[A-Z0-9]{2,10})/hist$", "ledger"),
        (r"^v2/auth/w/funding/offer/submit$", "submit"),
        (r"^v2/auth/w/funding/offer/cancel$", "cancel"),
        (r"^v2/auth/w/funding/offer/cancel/all$", "cancel_all"),
    )
)


def route(path: str) -> Route | None:
    for pattern, name in _ROUTES:
        match = pattern.match(path)
        if match is not None:
            return name, {k: v for k, v in match.groupdict().items() if v is not None}  # type: ignore[return-value]
    return None


def active_rows(state: VenueState, stream: str, symbol: str | None) -> list[list[Any]]:
    if stream == "offers":
        return [
            offer_row(o) for o in sorted(state.offers.values(), key=lambda o: o.offer_id)
            if o.resting and symbol in (None, o.symbol)
        ]
    kind = "credit" if stream == "credits" else "loan"
    return [
        lending_row(lend)
        for (k, _), lend in sorted(state.lendings.items(), key=lambda item: item[0][1])
        if k == kind and lend.active and symbol in (None, lend.symbol)
    ]


def wallet_rows(state: VenueState) -> list[list[Any]]:
    return [
        wallet_row(cur, wallet, state.available(cur))
        for cur, wallet in sorted(state.wallets.items())
    ]


@dataclass(frozen=True, slots=True)
class HistRow:
    ts: int  # the timestamp this stream is filtered and paged on
    ident: int
    row: list[Any]


def history_rows(
    state: VenueState, config: SimulatedVenueConfig, *, stream: str, symbol: str | None,
    now_ms: int,
) -> list[HistRow]:
    """Terminal rows visible at `now_ms`; a terminal row appears `history_lag_ms` late."""
    visible_before = now_ms - config.history_lag_ms
    if stream == "offers":
        use_update = config.history_filter.offers == "update"
        return [
            HistRow(o.mts_update if use_update else o.mts_create, o.offer_id, offer_row(o))
            for o in state.offers.values()
            if not o.resting and symbol in (None, o.symbol)
            and (o.terminal_mts or 0) <= visible_before
        ]
    if stream == "trades":
        return [
            HistRow(t.mts, t.trade_id, trade_row(t)) for t in state.trades
            if symbol in (None, t.symbol) and t.mts <= now_ms
        ]
    kind = "credit" if stream == "credits" else "loan"
    field = config.history_filter.credits if kind == "credit" else config.history_filter.loans
    return [
        HistRow(lend.mts_update if field == "update" else lend.mts_create,
                lend.lending_id, lending_row(lend))
        for (k, _), lend in state.lendings.items()
        if k == kind and not lend.active and symbol in (None, lend.symbol)
        and (lend.close_mts or 0) <= visible_before
    ]
