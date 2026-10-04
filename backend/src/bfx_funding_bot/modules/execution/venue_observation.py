"""Dormant Bitfinex observation adapter; not wired into the bot.

One budget covers both active reads and every symbol's four history streams.
The default 48 slots allow eight active requests plus 4 * 5 pages * 2 symbols.
Four slots are reserved for confirmation: history exhaustion must produce
incomplete evidence, rather than starve the mandatory active confirmation.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal

from bfx_funding_bot.core.venue_time import HISTORY_QUERY_MARGIN_MS
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.observations import (
    CoveredHistory,
    CreditObservation,
    ObservationRequestBudget,
    OfferObservation,
    TradeObservation,
    WalletObservation,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.ledger import (
    Coverage,
    Credit,
    CreditHistory,
    Observation,
    ObservationWindow,
    Offer,
    OfferHistory,
    OfferStatus,
    OfferTerminalKind,
    Scope,
    Trade,
    Wallet,
)
from bfx_funding_bot.modules.observability import alerts

HISTORY_PAGE_CAP = 5
# 8 active requests + 4 streams * 5 pages * 2 symbols, of which the last four stay reserved for
# the confirmation read. By-id lookups of vanished offers come out of the same budget:
# ceil(vanished offers of a symbol / 25) requests per symbol, so one vanished offer costs one
# request and the worst case is ceil(n / 25) + (symbols - 1) when n offers vanished at once.
# A budget that runs out is not a stall: the affected ids take the grace -> unconfirmed-end
# path (cause ``cap_exhausted``), where conservation judges them by trades.
OBSERVATION_REQUEST_CAP = 8 + 4 * HISTORY_PAGE_CAP * 2
HISTORY_INCOMPLETE_ALERT = "venue_observation_history_incomplete"
# Venue history may lag an offer's end. Whatever stops a vanished id from getting a terminal
# answer (not returned, request failed, cap exhausted, unparseable row) is retried each cycle
# inside this grace, measured in this process from the first sighting of that id without an
# answer (a restart starts a new one: only ever waits longer, so it still absorbs the lag).
OFFER_END_GRACE_MS = 120_000


def _clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _variant(status: str, word: str) -> bool:
    return status == word or status.startswith(word + " ")


def is_funding_wallet(wallet_type: str) -> bool:
    """Only funding wallets belong to the lending capital scope."""
    return wallet_type == "funding"


def normalize_wallet(row: WalletObservation) -> Wallet:
    # Null is not zero: the venue has not calculated the available balance.
    if row.available is None:
        raise ValueError("wallet available balance is unobserved")
    return Wallet(row.wallet_type, row.currency, row.available, row.balance,
                  "f" + row.currency if row.wallet_type == "funding" else None)


def _json_safe(value: object) -> object:
    """The venue row as JSON: ``parse_float=Decimal`` made the numbers exact Decimals, and
    the ledger's ``raw`` is a ``JsonObject``. Exact decimals travel as strings
    (``format(v, "f")``, the wire digits kept: ``-150.0`` stays ``-150.0``)."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _offer(row: OfferObservation, status: OfferStatus) -> Offer:
    flags = row.raw[9]
    return Offer(
        venue_offer_id=row.venue_offer_id, symbol=row.symbol,
        amount_original=row.amount_original, amount_remaining=row.amount,
        rate=row.rate, rate_observed=row.rate is not None, period_days=row.period_days,
        offer_type=str(row.raw[6]) if row.raw[6] is not None else None,
        flags=flags if isinstance(flags, (dict, int)) else None,
        status=status, mts_created=row.mts_created, mts_updated=row.mts_updated,
        raw={"row": _json_safe(row.raw)},
    )


def normalize_offer(row: OfferObservation) -> Offer:
    if row.status == "ACTIVE":
        return _offer(row, "active")
    if _variant(row.status, "PARTIALLY FILLED"):
        return _offer(row, "partially_filled")
    raise ValueError(f"unknown active offer status: {row.status}")


def normalize_offer_history(row: OfferObservation) -> OfferHistory:
    kind: OfferTerminalKind
    if _variant(row.status, "EXECUTED"):
        kind = "executed"
    elif _variant(row.status, "CANCELED") or row.status == "EXPIRED":
        kind = "canceled"
    else:
        raise ValueError(f"unknown offer history status: {row.status}")
    status: OfferStatus = (
        "partially_filled" if "was: PARTIALLY FILLED" in row.status else "active"
    )
    return OfferHistory(_offer(row, status), kind, row.mts_updated)


def _credit(row: CreditObservation) -> Credit:
    if not isinstance(row.mts_opening, int) or row.mts_opening < 0:
        raise ValueError("credit/loan MTS_OPENING is required")
    flags = row.raw[6]
    return Credit(
        source_kind=row.source_kind, venue_credit_id=row.credit_id, symbol=row.symbol,
        amount=row.amount, rate=row.rate, period_days=row.period_days, status="active",
        flags=flags if isinstance(flags, (dict, int)) else None,
        mts_created=row.mts_created, mts_updated=row.mts_updated,
        mts_opening=row.mts_opening, raw={"row": _json_safe(row.raw)},
    )


def normalize_credit(row: CreditObservation) -> Credit:
    if row.status != "ACTIVE":
        raise ValueError(f"unknown active {row.source_kind} status: {row.status}")
    return _credit(row)


def normalize_credit_history(row: CreditObservation) -> CreditHistory:
    # Any close reason ends the credit; production history holds "CLOSED" and
    # "CLOSED (no more position)" besides used/expired/reduced.
    if not _variant(row.status, "CLOSED"):
        raise ValueError(f"unknown {row.source_kind} history status: {row.status}")
    return CreditHistory(_credit(row), "closed", row.mts_updated)


def normalize_trade(row: TradeObservation) -> Trade:
    return Trade(row.trade_id, row.symbol, str(row.offer_id), row.amount,
                 row.rate, row.period_days, row.mts_create, row.maker)


@dataclass(frozen=True, slots=True)
class _OfferEnds:
    """One symbol's offer-end evidence: the windowed query and the by-id lookups, kept apart."""

    windowed: CoveredHistory[OfferHistory]
    by_id: tuple[OfferHistory, ...]  # terminal rows found by id that the window did not carry
    resolved: bool  # every vanished id has a terminal row or is an unconfirmed end
    unconfirmed: tuple[str, ...]
    missing: frozenset[str]  # ids without a terminal answer this cycle (grace bookkeeping)


class BitfinexVenueObservation:
    """A context bound to one account/realm by the future composition root.

    Sequential reads preserve the mandated order; there are no retries here.
    Only the additive, covered REST path is used. Each incomplete stream emits
    an alert through the normal non-blocking observability sink.
    """

    def __init__(
        self, *, rest: BitfinexAuthREST, ctx: AccountContext, scope: Scope,
        clock_ms: Callable[[], int] = _clock_ms,
        request_cap: int = OBSERVATION_REQUEST_CAP,
    ) -> None:
        if ctx.account_id != str(scope.exchange_account_id):
            raise ValueError("venue observation context account does not match scope")
        if request_cap < 8:
            raise ValueError("observation budget must cover both active reads")
        self._rest = rest
        self._ctx = ctx
        self._scope = scope
        self._clock_ms = clock_ms
        self._request_cap = request_cap
        # The only state kept across cycles: offer id -> first sighting without a terminal
        # answer. Alert de-duplication is the alerts registry's (DEDUP_FIELDS).
        self._missing_since: dict[str, int] = {}

    async def _active(self, budget: ObservationRequestBudget) -> Observation:
        # Only funding wallets are this system's capital. Exchange/margin
        # wallets would put unrelated churn (and venue-null balances) into
        # the active digest, failing acceptance for no capital reason.
        wallets = tuple(normalize_wallet(row) for row in
                        await self._rest.fetch_wallet_observations(ctx=self._ctx, budget=budget)
                        if is_funding_wallet(row.wallet_type))
        offers = tuple(normalize_offer(row) for row in
                       await self._rest.fetch_active_offer_observations(ctx=self._ctx, budget=budget))
        credits = tuple(normalize_credit(row) for row in
                        await self._rest.fetch_active_credit_observations(ctx=self._ctx, budget=budget))
        loans = tuple(normalize_credit(row) for row in
                      await self._rest.fetch_active_loan_observations(ctx=self._ctx, budget=budget))
        return Observation(
            wallets, offers, credits + loans,
            Coverage(True, True, True, True, False, False, 1, 1, 1, 1, 0, 0),
            self._clock_ms(),
        )

    async def _history[Row, Normalized](
        self, fetch: Callable[..., Awaitable[CoveredHistory[Row]]],
        normalize: Callable[[Row], Normalized], *, stream: str, symbol: str,
        start_ms: int, end_ms: int, budget: ObservationRequestBudget,
    ) -> CoveredHistory[Normalized]:
        before = budget.requests_used
        try:
            covered = await fetch(
                ctx=self._ctx, symbol=symbol, start_ms=start_ms, end_ms=end_ms,
                max_pages=HISTORY_PAGE_CAP, budget=budget,
            )
        except Exception as exc:
            # A failed pager cannot certify any range. Count attempted requests,
            # including the failed page; do not retain uncertified partial rows.
            self._alert(stream, symbol, reason=type(exc).__name__)
            return CoveredHistory((), False, budget.requests_used - before, start_ms, end_ms)
        rows: list[Normalized] = []
        complete = covered.complete
        for row in covered.rows:
            try:
                rows.append(normalize(row))
            except ValueError as exc:
                complete = False
                self._alert(stream, symbol, reason=str(exc))
        if not covered.complete:
            self._alert(stream, symbol, reason="coverage_incomplete")
        return CoveredHistory(tuple(rows), complete, covered.pages,
                              covered.requested_start_ms, covered.requested_end_ms)

    async def _offer_history(
        self, symbol: str, vanished: list[str], *, start_ms: int, end_ms: int,
        budget: ObservationRequestBudget,
    ) -> _OfferEnds:
        """The windowed query, plus the vanished offers as point lookups by id.

        The two stay separate evidence: coverage describes the windowed query only. Absence
        never confirms terminal: a vanished id with no terminal answer (``not_returned``,
        ``request_failed``, ``cap_exhausted`` or an ``unparseable`` row) is retried inside
        the grace, which keeps this symbol incomplete; after it the id is an unconfirmed end
        and conservation decides from the trades.
        """
        windowed = await self._history(
            self._rest.fetch_offer_history_observations, normalize_offer_history,
            stream="offers", symbol=symbol, start_ms=start_ms, end_ms=end_ms, budget=budget)
        have = {item.offer.venue_offer_id for item in windowed.rows}
        # The window already carries some terminal rows; only the others need a lookup.
        need = sorted(set(vanished) - have)
        if not need:
            return _OfferEnds(windowed, (), True, (), frozenset())
        lookup = await self._rest.fetch_offer_history_by_ids(
            ctx=self._ctx, symbol=symbol, offer_ids=need, budget=budget)
        by_id: list[OfferHistory] = []
        unconfirmed: list[str] = []
        missing: set[str] = set()
        resolved = True
        for offer_id in need:
            cause = lookup.failed.get(offer_id)
            row = lookup.found.get(offer_id)
            if cause is None and row is None:
                cause = "not_returned"
            if cause is None:
                assert row is not None
                try:
                    by_id.append(normalize_offer_history(row))
                except ValueError:
                    cause = "unparseable"
            if cause is None:
                continue
            missing.add(offer_id)
            if self._grace_over(offer_id):
                unconfirmed.append(offer_id)
                alerts.emit(alerts.OFFER_END_UNCONFIRMED, level=alerts.WARNING,
                            exchange_account_id=str(self._scope.exchange_account_id),
                            deployment_environment=self._scope.deployment_environment,
                            symbol=symbol, venue_offer_id=offer_id, cause=cause,
                            grace_ms=OFFER_END_GRACE_MS)
            else:
                resolved = False
                self._alert("offers_by_id", symbol, reason=cause)
        return _OfferEnds(windowed, tuple(by_id), resolved, tuple(unconfirmed), frozenset(missing))

    def _grace_over(self, offer_id: str) -> bool:
        """The first sighting without an answer starts the grace; true once it has run out."""
        now = self._clock_ms()
        first = self._missing_since.setdefault(offer_id, now)
        return now - first >= OFFER_END_GRACE_MS

    def _alert(self, stream: str, symbol: str, *, reason: str) -> None:
        alerts.emit(HISTORY_INCOMPLETE_ALERT, level=alerts.WARNING,
                    exchange_account_id=str(self._scope.exchange_account_id),
                    deployment_environment=self._scope.deployment_environment,
                    stream=stream, symbol=symbol, reason=reason)

    async def observe(
        self, scope: Scope, query_started_at_ms: int, window: ObservationWindow,
    ) -> tuple[Observation, Observation, int]:
        if scope != self._scope:
            raise ValueError("venue observation scope mismatch")
        # Reserve the final four requests without introducing a second budget.
        budget = ObservationRequestBudget(self._request_cap - 4)
        active = await self._active(budget)
        end_ms = active.finished_at_ms  # local time after all four streams
        start_ms = window.history_start_ms
        if start_ms is None:
            start_ms = query_started_at_ms - HISTORY_QUERY_MARGIN_MS
        # Credits opened before the previous accepted query were in that basis
        # and are carried by (symbol, period, opening): they need no trades.
        # Later openings fall after start_ms. Only with no accepted query yet
        # must trades reach back to every current opening.
        if window.previous_query_started_at_ms is None and active.credits:
            trade_start_ms = min(start_ms, *(credit.mts_opening - HISTORY_QUERY_MARGIN_MS
                                             for credit in active.credits))
        else:
            trade_start_ms = start_ms
        # Wallet and credit symbols are the capital; the window's anchor attempts add
        # the symbols an UNKNOWN or R6 absence proof needs even when no wallet names them.
        # P's live offers that are gone from this active read ended somewhere in
        # (P, now]: conservation needs each one's terminal row, however old the offer.
        # The windowed stream cannot certify that, so ask the venue by id.
        active_ids = {offer.venue_offer_id for offer in active.offers}
        vanished: dict[str, list[str]] = {}
        for live in window.live_offers:
            if live.venue_offer_id not in active_ids:
                vanished.setdefault(live.symbol, []).append(live.venue_offer_id)
        symbols = sorted({wallet.symbol for wallet in active.wallets if wallet.symbol is not None}
                         | {credit.symbol for credit in active.credits}
                         | window.anchor_symbols | vanished.keys())
        ends: list[_OfferEnds] = []
        credits: list[CoveredHistory[CreditHistory]] = []
        trades: list[CoveredHistory[Trade]] = []
        for symbol in symbols:
            ends.append(await self._offer_history(
                symbol, vanished.get(symbol, []), start_ms=start_ms, end_ms=end_ms, budget=budget))
            credits.append(await self._history(
                self._rest.fetch_credit_history_observations, normalize_credit_history,
                stream="credits", symbol=symbol, start_ms=start_ms, end_ms=end_ms, budget=budget))
            credits.append(await self._history(
                self._rest.fetch_loan_history_observations, normalize_credit_history,
                stream="loans", symbol=symbol, start_ms=start_ms, end_ms=end_ms, budget=budget))
            trades.append(await self._history(
                self._rest.fetch_trade_observations, normalize_trade,
                stream="trades", symbol=symbol, start_ms=trade_start_ms, end_ms=end_ms, budget=budget))
        offers = [end.windowed for end in ends]  # coverage: the windowed query only
        history: list[CoveredHistory[OfferHistory] | CoveredHistory[CreditHistory]] = [*offers, *credits]
        # R-e: one shared requested range; never manufacture a broader range
        # than the covered pager reports. A mismatched range fails closed.
        history_range_matches = all((h.requested_start_ms, h.requested_end_ms) ==
                                    (start_ms, end_ms) for h in history)
        trade_range_matches = all((h.requested_start_ms, h.requested_end_ms) ==
                                  (trade_start_ms, end_ms) for h in trades)
        created = [h.offer.mts_created for stream in offers for h in stream.rows]
        created.extend(h.credit.mts_created for stream in credits for h in stream.rows
                       if h.credit.mts_created is not None)
        coverage = replace(
            active.coverage,
            offer_history_complete=(history_range_matches and all(h.complete for h in offers)
                                    and all(end.resolved for end in ends)),
            credit_history_complete=history_range_matches and all(h.complete for h in credits),
            offer_history_pages=sum(h.pages for h in offers),
            credit_history_pages=sum(h.pages for h in credits),
            history_requested_start_ms=history[0].requested_start_ms if history else start_ms,
            history_requested_end_ms=history[0].requested_end_ms if history else end_ms,
            history_oldest_mts_created=min(created) if created else None,
            history_newest_mts_created=max(created) if created else None,
            trades_complete=trade_range_matches and all(h.complete for h in trades),
            trades_requested_start_ms=trades[0].requested_start_ms if trades else trade_start_ms,
            trades_requested_end_ms=trades[0].requested_end_ms if trades else end_ms,
            history_symbols=frozenset(symbols),
        )
        # An id that is back, or no longer vanished, starts from scratch next time.
        missing = frozenset().union(*(end.missing for end in ends))
        self._missing_since = {i: t for i, t in self._missing_since.items() if i in missing}
        first = replace(active, coverage=coverage, finished_at_ms=self._clock_ms(),
                        unconfirmed_ends=tuple(sorted(i for end in ends for i in end.unconfirmed)),
                        offer_history=tuple(row for end in ends
                                            for row in (*end.windowed.rows, *end.by_id)),
                        credit_history=tuple(row for h in credits for row in h.rows),
                        trades=tuple(row for h in trades for row in h.rows))
        confirmation_started_at_ms = self._clock_ms()
        budget.request_cap = self._request_cap
        confirmation = await self._active(budget)
        return first, confirmation, confirmation_started_at_ms
