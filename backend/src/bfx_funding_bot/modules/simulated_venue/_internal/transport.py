"""The simulated Bitfinex: an `httpx` transport over an event-sourced venue.

One request at a time under a lock: authenticate, catch up every scheduled change
due by the injected clock, answer from that one consistent state. Writes are
durable before the response is produced, so a crash or a lost response leaves
exactly what a real venue would: the order placed, the caller unsure.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from bfx_funding_bot.modules.simulated_venue._internal import decide, wire
from bfx_funding_bot.modules.simulated_venue._internal.auth import RequestAuthenticator
from bfx_funding_bot.modules.simulated_venue._internal.faults import FaultInjector
from bfx_funding_bot.modules.simulated_venue._internal.state import VenueState, apply
from bfx_funding_bot.modules.simulated_venue.contracts import (
    ConcurrentAppendError,
    FaultKind,
    FaultPlan,
    FaultTarget,
    FeedFailureError,
    InternalFailure,
    MarketFeed,
    NoMarketDataError,
    PublicTrade,
    SimAccount,
    SimulatedVenueConfig,
    SimulatedVenueInternalError,
    VenueEventStore,
    VenueObserver,
    VenueStoreError,
)
from bfx_funding_bot.modules.simulated_venue.events import NonceAdvanced, VenueEvent

log = logging.getLogger(__name__)

HOST = "api.bitfinex.com"
_ERR_GENERIC = 10001
_ERR_STORAGE = 10000
_TARGETS = {
    "submit": FaultTarget.SUBMIT, "cancel": FaultTarget.CANCEL,
    "cancel_all": FaultTarget.CANCEL_ALL, "hist": FaultTarget.HISTORY,
}


# A soak runs for days: the in-memory logs keep the newest entries, the counters (here and in
# the injected observer) keep the totals.
LOG_LIMIT = 1000


class BoundedLog[T](deque[T]):
    """A bounded log that compares equal to the list of what it holds (``== []`` in tests)."""

    def __init__(self) -> None:
        super().__init__(maxlen=LOG_LIMIT)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple, deque)):
            return list(self) == list(other)
        return NotImplemented

    __hash__ = None


class _BadRequestError(ValueError):
    pass


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(
        status, content=wire.dumps(body), headers={"content-type": "application/json"},
    )


def _body_object(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError as exc:
        raise _BadRequestError("body is not JSON") from exc
    if not isinstance(parsed, dict):
        raise _BadRequestError("body must be a JSON object")
    return parsed


class SimulatedVenue(httpx.AsyncBaseTransport):
    """Use `client()` as the `http=` of the auth REST client and the executors."""

    def __init__(
        self, *, account: SimAccount, config: SimulatedVenueConfig, store: VenueEventStore,
        feed: MarketFeed, clock_ms: Callable[[], int], faults: FaultPlan,
        state: VenueState, seq: int, observer: VenueObserver | None = None,
    ) -> None:
        self._account = account
        self._config = config
        self._store = store
        self._feed = feed
        self._clock_ms = clock_ms
        self._faults = FaultInjector(faults)
        self._auth = RequestAuthenticator(config.api_key, config.api_secret)
        self._state = state
        self._seq = seq
        self._lock = asyncio.Lock()
        self.requests = 0
        self._observer = observer
        self.unexpected: BoundedLog[tuple[str, str]] = BoundedLog()
        self.unexpected_total = 0
        # Simulator-internal failures (never venue answers). CI asserts this is empty at
        # teardown and the soak report counts it apart from injected venue faults.
        self.internal_failures: BoundedLog[InternalFailure] = BoundedLog()
        self.internal_failures_total = 0

    @classmethod
    async def open(
        cls, *, account: SimAccount, config: SimulatedVenueConfig, store: VenueEventStore,
        feed: MarketFeed, clock_ms: Callable[[], int], faults: FaultPlan | None = None,
        observer: VenueObserver | None = None,
    ) -> SimulatedVenue:
        """Rebuild the venue from its durable log (a restart answers identically)."""
        state = VenueState()
        events = await store.load(account)
        for event in events:
            apply(state, event)
        return cls(
            account=account, config=config, store=store, feed=feed, clock_ms=clock_ms,
            faults=faults or FaultPlan(), state=state, seq=len(events), observer=observer,
        )

    @property
    def state(self) -> VenueState:
        """Read-only view of the live state (tests and invariants)."""
        return self._state

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self)

    async def fund_wallet(self, currency: str, amount: Decimal) -> None:
        async with self._lock:
            await self._commit(decide.fund_wallet(currency, amount, self._now()))

    async def fund_wallets_if_empty(self, wallets: Mapping[str, Decimal]) -> bool:
        """Seed the wallets in ONE append, and only on a venue whose log is empty.

        A restart finds the log non-empty and funds nothing, so the wallet is never funded
        twice; two processes racing on an empty log lose with `ConcurrentAppendError`
        (the append is conditional on `expected_seq=0`). True when this call funded.
        """
        async with self._lock:
            if self._seq != 0 or not wallets:
                return False
            now = self._now()
            await self._commit([
                event for currency, amount in sorted(wallets.items())
                for event in decide.fund_wallet(currency, amount, now)
            ])
            return True

    async def aclose(self) -> None:
        return None

    # -- request handling ---------------------------------------------------

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with self._lock:
            self.requests += 1
            try:
                return await self._handle(request)
            finally:
                await self._after_request()

    def _now(self) -> int:
        return max(self._clock_ms(), self._state.high_water_ms)

    def _record_internal(self, kind: str, detail: str) -> None:
        self.internal_failures.append(InternalFailure(kind, detail))
        self.internal_failures_total += 1
        if self._observer is not None:
            self._observer.internal_failure(kind)
        log.critical("simulated venue internal failure kind=%s detail=%s", kind, detail)

    async def _after_request(self) -> None:
        """TICK_AFTER rules: the world moves between two requests of one caller."""
        try:
            due = self._faults.ticks_after(self.requests)
            for rule in due:
                if rule.hook is not None:
                    rule.hook()
            if due:
                await self._sync(self._now())
        except SimulatedVenueInternalError as exc:
            self._record_internal(exc.kind, str(exc))
        except VenueStoreError as exc:
            self._record_internal("store", repr(exc))
        except Exception as exc:
            self._record_internal("bug", repr(exc))

    async def _reload(self) -> None:
        """Rebuild state from the durable log (after losing an append race)."""
        state = VenueState()
        events = await self._store.load(self._account)
        for event in events:
            apply(state, event)
        self._state, self._seq = state, len(events)

    async def _commit(self, events: Sequence[VenueEvent]) -> None:
        if not events:
            return
        try:
            await self._store.append(self._account, self._seq, events)
        except ConcurrentAppendError:
            await self._reload()
            raise
        except VenueStoreError:
            raise
        except Exception as exc:
            raise VenueStoreError(f"append failed: {exc!r}") from exc
        for event in events:
            apply(self._state, event)
        self._seq += len(events)

    async def _feed_call[T](self, what: str, call: Awaitable[T]) -> T:
        """Feed reads run inside the venue lock, so they must be local and fast."""
        try:
            async with asyncio.timeout(self._config.feed_deadline_s):
                return await call
        except TimeoutError as exc:
            raise FeedFailureError(
                f"feed {what} exceeded {self._config.feed_deadline_s}s: a MarketFeed must "
                "not do network I/O inside the venue lock") from exc
        except Exception as exc:
            raise FeedFailureError(f"feed {what} failed: {exc!r}") from exc

    async def _sync(self, now_ms: int, nonce: int | None = None) -> None:
        """Record the accepted nonce, then every scheduled change due by `now_ms`.

        Public trades are consumed only up to the feed's event-time watermark: the range
        `(market_through, min(now, watermark)]` is final, so a trade that reaches the feed
        later with an older timestamp is still in a range nobody has consumed yet. A symbol
        whose watermark is unknown consumes nothing and moves nothing.
        """
        trades: dict[str, Sequence[PublicTrade]] = {}
        through: dict[str, int] = {}
        for symbol in sorted({o.symbol for o in self._state.offers.values() if o.resting}):
            mark = await self._feed_call("watermark", self._feed.complete_through(symbol))
            if mark is None:
                continue
            upto = min(now_ms, mark)
            after = self._state.market_through.get(symbol, now_ms)
            if upto <= after:
                continue
            through[symbol] = upto
            trades[symbol] = await self._feed_call("trades", self._feed.trades(
                symbol, after_ms=after, through_ms=upto))
        events: list[VenueEvent] = [] if nonce is None else [NonceAdvanced(nonce, now_ms)]
        events += decide.catch_up(
            self._state, self._config, now_ms=now_ms, trades=trades, through=through)
        await self._commit(events)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        routed = wire.route(path)
        if (request.method != "POST" or request.url.scheme != "https"
                or request.url.host != HOST or routed is None):
            self.unexpected.append((request.method, str(request.url)))
            self.unexpected_total += 1
            if self._observer is not None:
                self._observer.unexpected_request()
            return _json(404, wire.error_body(404, "simulated venue: unexpected request"))
        name, params = routed
        body = await request.aread()
        accepted = self._auth.check(
            path=path, body=body, headers=request.headers, last_nonce=self._state.last_nonce)
        if isinstance(accepted, tuple):
            return _json(500, wire.error_body(*accepted))
        fault = self._faults.next_fault(_TARGETS[name]) if name in _TARGETS else None
        if fault is FaultKind.UNKNOWN_NOT_PLACED_LOST:
            raise httpx.ConnectError("simulated: request lost before the venue", request=request)
        try:
            await self._sync(self._now(), accepted)
            payload = _body_object(body)
            return await self._dispatch(name, params, payload, fault, request)
        except _BadRequestError as exc:
            return _json(500, wire.error_body(_ERR_GENERIC, f"bad request: {exc}"))
        except httpx.HTTPError:
            raise
        except VenueStoreError as exc:
            # A durable-write failure is the one internal failure that is also a venue
            # behaviour ("not recorded, caller unsure"): 5xx, and counted as internal.
            self._record_internal("store", repr(exc))
            return _json(500, wire.error_body(_ERR_STORAGE, "simulated venue: write failed"))
        except SimulatedVenueInternalError as exc:
            self._record_internal(exc.kind, str(exc))
            raise
        except Exception as exc:
            self._record_internal("bug", repr(exc))
            raise SimulatedVenueInternalError(f"simulator bug: {exc!r}") from exc

    async def _dispatch(
        self, name: str, params: dict[str, str], payload: dict[str, Any],
        fault: FaultKind | None, request: httpx.Request,
    ) -> httpx.Response:
        now = self._now()
        if name == "wallets":
            return _json(200, wire.wallet_rows(self._state))
        if name == "active":
            return _json(200, wire.active_rows(
                self._state, params["stream"], params.get("symbol")))
        if name == "hist":
            return self._history(params, payload, fault, now)
        if name == "ledger":
            return self._ledger(params["currency"], payload, now)
        return await self._write(name, payload, fault, request, now)

    # -- reads --------------------------------------------------------------

    def _page_request(self, payload: dict[str, Any]) -> wire.PageRequest:
        try:
            return wire.parse_page_request(payload, max_limit=self._config.history_max_limit)
        except ValueError as exc:
            raise _BadRequestError(str(exc)) from exc

    def _history(
        self, params: dict[str, str], payload: dict[str, Any], fault: FaultKind | None,
        now: int,
    ) -> httpx.Response:
        if fault is FaultKind.HISTORY_ERROR:
            return _json(503, wire.error_body(_ERR_GENERIC, "simulated: history unavailable"))
        request = self._page_request(payload)
        candidates = wire.history_rows(
            self._state, self._config, stream=params["stream"],
            symbol=params.get("symbol"), now_ms=now,
        )
        if "id" in payload:
            # By id (probed on the live account, offers only): exactly those ended offers,
            # whatever their timestamps; ids the venue does not know are simply absent.
            if params["stream"] != "offers":
                raise _BadRequestError("id is supported for offers only")
            ids = payload["id"]
            if not isinstance(ids, list) or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in ids
            ):
                raise _BadRequestError("id must be a list of integers")
            candidates = [r for r in candidates if r.ident in ids]
            request = wire.PageRequest(None, None, request.limit, request.ascending)
        rows = wire.page(candidates, request=request, ts=lambda r: r.ts, ident=lambda r: r.ident)
        if fault is FaultKind.HISTORY_OMIT_NEWEST and rows:
            newest = max(rows, key=lambda r: (r.ts, r.ident))
            rows = [r for r in rows if r is not newest]
        return _json(200, [r.row for r in rows])

    def _ledger(self, currency: str, payload: dict[str, Any], now: int) -> httpx.Response:
        if payload.get("category") not in (None, 28):
            return _json(200, [])
        rows = wire.page(
            [e for e in self._state.ledger if e.currency == currency and e.mts <= now],
            request=self._page_request(payload), ts=lambda e: e.mts, ident=lambda e: e.ledger_id,
        )
        return _json(200, [wire.ledger_row(e) for e in rows])

    # -- writes -------------------------------------------------------------

    def _business_error(self, type_: str, reason: str, now: int) -> httpx.Response:
        if self._config.business_rejection == "5xx":
            return _json(500, wire.error_body(_ERR_GENERIC, reason))
        return _json(200, wire.notification(now, type_, None, "ERROR", reason))

    def _fault_response(
        self, fault: FaultKind | None, type_: str, now: int,
    ) -> httpx.Response | None:
        """Faults that stop a write before it takes effect."""
        if fault is FaultKind.UNKNOWN_5XX_ERROR:
            return _json(500, wire.error_body(_ERR_GENERIC, "simulated: venue error"))
        if fault is FaultKind.REJECTED:
            return _json(200, wire.notification(
                now, type_, None, "ERROR", "simulated: rejected by the venue"))
        return None

    async def _write(
        self, name: str, payload: dict[str, Any], fault: FaultKind | None,
        request: httpx.Request, now: int,
    ) -> httpx.Response:
        type_ = {"submit": "fon-req", "cancel": "foc-req", "cancel_all": "foc_all-req"}[name]
        stopped = self._fault_response(fault, type_, now)
        if stopped is not None:
            return stopped
        response = await self._apply_write(name, type_, payload, now)
        if fault is FaultKind.UNKNOWN_PLACED_LOST:
            raise httpx.ReadTimeout("simulated: response lost after commit", request=request)
        return response

    async def _apply_write(
        self, name: str, type_: str, payload: dict[str, Any], now: int,
    ) -> httpx.Response:
        if name == "submit":
            try:
                if payload.get("type") != "LIMIT":
                    raise _BadRequestError("only LIMIT funding offers are simulated")
                amount = Decimal(str(payload["amount"]))
                rate = Decimal(str(payload["rate"]))
                period, symbol = payload["period"], payload["symbol"]
                if isinstance(period, bool) or not isinstance(period, int) or not isinstance(
                        symbol, str):
                    raise _BadRequestError("period must be an integer and symbol a string")
            except (KeyError, InvalidOperation) as exc:
                raise _BadRequestError(f"invalid submit payload: {exc!r}") from exc
            if amount <= 0:
                return self._business_error(type_, "amount: borrowing is not simulated", now)
            book = await self._feed_call("book", self._feed.book(symbol, at_ms=now))
            decided = decide.decide_submit(
                self._state, self._config, now_ms=now, symbol=symbol, amount=amount,
                rate=rate, period=period, book=book,
            )
            if isinstance(decided, decide.NoMarketData):
                raise NoMarketDataError(decided.reason)  # ours to fix, not a venue answer
            if isinstance(decided, decide.Refusal):
                return self._business_error(type_, decided.reason, now)
            await self._commit(decided)
            offer = self._state.offers[next(
                e.offer_id for e in decided if hasattr(e, "offer_id"))]
            return _json(200, wire.notification(
                now, type_, wire.offer_row(offer), "SUCCESS",
                f"Submitting funding offer of {offer.amount_original} {offer.symbol[1:]} "
                f"at {offer.rate * 100:f}% for {offer.period} days.",
            ))
        if name == "cancel":
            offer_id = payload.get("id")
            if isinstance(offer_id, bool) or not isinstance(offer_id, int):
                raise _BadRequestError("id must be an integer")
            cancelled = decide.decide_cancel(self._state, now_ms=now, offer_id=offer_id)
            if isinstance(cancelled, decide.Refusal):
                if self._config.cancel_rejection == "5xx":
                    return _json(500, wire.error_body(_ERR_GENERIC, cancelled.reason))
                return _json(200, wire.notification(now, type_, None, "ERROR", cancelled.reason))
            await self._commit(cancelled)
            return _json(200, wire.notification(
                now, type_, wire.offer_row(self._state.offers[offer_id]), "SUCCESS",
                "Funding offer cancelled.",
            ))
        currency = payload.get("currency")
        if not isinstance(currency, str) or not currency:
            raise _BadRequestError("currency must be a string")
        await self._commit(decide.decide_cancel_all(self._state, now_ms=now, currency=currency))
        return _json(200, wire.notification(
            now, type_, None, "SUCCESS", "Submitting funding offer cancellations."))
