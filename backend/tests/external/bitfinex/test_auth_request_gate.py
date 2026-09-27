"""Signed requests on one API key reach the venue in nonce order.

Bitfinex rejects a nonce smaller than the last one it *received* on the key
(HTTP 500, ``["error", CODE, "nonce: small"]``). A monotonic generator alone
does not prevent that: two callers take N and N+1, then race over the network.
Production 2026-09-27 07:09:04Z: the reconcile's credits read and the
CreditHistorySync trades read overlapped and one got HTTP 500.

The fake venue below rejects exactly like that, and lets each test choose when
an in-flight request "arrives", so the race is deterministic.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.external.bitfinex.live_executor import FundingCancelAllClient
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate, GateKind
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

_KEY = "the-api-key-value"
_SECRET = "the-api-secret-value"
_CTX = AccountContext("acct", Credentials(_KEY, _SECRET), Decimal("0"))
_NONCE_SMALL = ["error", 10114, "nonce: small"]
_CANCEL_ALL_OK = [1, "foc_all-req", None, None, None, None, "SUCCESS", "ok"]


def _trade_row(trade_id: int, mts: int) -> list[object]:
    return [trade_id, "fUST", mts, 1, "10", "0.0002", 2, None]


class _Venue:
    """Per-key nonce check at arrival time, with holdable request paths."""

    def __init__(self) -> None:
        self.last_nonce = 0
        self.arrivals: list[tuple[str, int]] = []
        self.accepted: list[str] = []
        self._holds: dict[str, asyncio.Event] = {}
        self.in_flight: dict[str, asyncio.Event] = {}
        self.responses: dict[str, Callable[[int], object]] = {}
        self.fail: dict[str, BaseException] = {}

    def hold(self, path_part: str) -> asyncio.Event:
        """The first request whose path contains ``path_part`` is delayed on the
        network until the returned event is set."""
        self._holds[path_part] = asyncio.Event()
        self.in_flight[path_part] = asyncio.Event()
        return self._holds[path_part]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        nonce = int(request.headers["bfx-nonce"])
        for part, event in list(self._holds.items()):
            if part in path:
                del self._holds[part]
                self.in_flight[part].set()
                await event.wait()
        for part, exc in list(self.fail.items()):
            if part in path:
                del self.fail[part]
                raise exc
        self.arrivals.append((path, nonce))
        if nonce <= self.last_nonce:
            return httpx.Response(500, json=_NONCE_SMALL)
        self.last_nonce = nonce
        self.accepted.append(path)
        for part, make in self.responses.items():
            if part in path:
                return httpx.Response(200, json=make(len(self.accepted)))
        return httpx.Response(200, json=[])


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _race(
    venue: _Venue, *, first: Awaitable[object], first_path: str, second: Awaitable[object],
) -> tuple[object, object]:
    """Start ``first`` and let it go in flight (held), start ``second``, then
    let ``first`` arrive. Without the gate ``second`` overtakes it."""
    release = venue.hold(first_path)
    t1 = asyncio.ensure_future(first)
    await asyncio.wait_for(venue.in_flight[first_path].wait(), 1)
    t2 = asyncio.ensure_future(second)
    await _settle()
    release.set()
    r1, r2 = await asyncio.gather(t1, t2, return_exceptions=True)
    return r1, r2


@pytest.mark.asyncio
async def test_concurrent_reads_on_one_key_arrive_in_nonce_order() -> None:
    """Regression: reconcile credits read vs CreditHistorySync trades read.

    On the pre-gate code the held credits read (smaller nonce) lands after the
    trades read and the venue answers 500 "nonce: small"."""
    venue = _Venue()
    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        reconcile = BitfinexAuthREST(http=http, auth_gate=gate)
        history = BitfinexAuthREST(http=http, auth_gate=gate)
        credits, trades = await _race(
            venue,
            first=reconcile.get_active_funding_credits(ctx=_CTX),
            first_path="funding/credits",
            second=history.get_funding_trades(
                ctx=_CTX, symbol="fUST", start_ms=0, end_ms=10_000,
            ),
        )
    assert credits == []
    assert trades == []
    nonces = [n for _, n in venue.arrivals]
    assert nonces == sorted(nonces)
    assert venue.accepted == [
        "/v2/auth/r/funding/credits", "/v2/auth/r/funding/trades/fUST/hist",
    ]


@pytest.mark.asyncio
async def test_trading_write_cannot_lose_the_race_to_a_read() -> None:
    """The kill path's cancel-all shares the gate with the auth reads."""
    venue = _Venue()
    venue.responses["cancel/all"] = lambda _: _CANCEL_ALL_OK
    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        reads = BitfinexAuthREST(http=http, auth_gate=gate)
        writes = FundingCancelAllClient(http=http, auth_gate=gate)
        _, result = await _race(
            venue,
            first=reads.get_funding_available_all(ctx=_CTX),
            first_path="wallets",
            second=writes.cancel_all_funding_offers(currency="UST", ctx=_CTX),
        )
    assert not isinstance(result, BaseException), result
    assert result.outcome == "acknowledged"  # type: ignore[attr-defined]
    nonces = [n for _, n in venue.arrivals]
    assert nonces == sorted(nonces)


@pytest.mark.asyncio
async def test_paging_releases_the_gate_between_pages() -> None:
    """A long history read holds the gate per page, so a write queued during
    page 1 goes out before page 2 instead of waiting for the whole loop."""
    venue = _Venue()
    # limit=1: every page is full, cursor walks back by 1000 ms per page.
    venue.responses["funding/trades"] = lambda n: [_trade_row(n, 10_000 - n * 1000)]
    venue.responses["cancel/all"] = lambda _: _CANCEL_ALL_OK
    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        reads = BitfinexAuthREST(http=http, auth_gate=gate)
        writes = FundingCancelAllClient(http=http, auth_gate=gate)
        release = venue.hold("funding/trades")
        paging = asyncio.ensure_future(reads.get_funding_trades(
            ctx=_CTX, symbol="fUST", start_ms=0, end_ms=10_000, limit=1, max_pages=3,
        ))
        await asyncio.wait_for(venue.in_flight["funding/trades"].wait(), 1)
        write = asyncio.ensure_future(
            writes.cancel_all_funding_offers(currency="UST", ctx=_CTX),
        )
        await _settle()
        release.set()
        await asyncio.gather(paging, write)
    kinds = ["cancel" if "cancel" in p else "page" for p in venue.accepted]
    assert kinds == ["page", "cancel", "page", "page"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"),
])
async def test_transport_failure_releases_the_gate(exc: BaseException) -> None:
    venue = _Venue()
    venue.fail["funding/credits"] = exc
    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        client = BitfinexAuthREST(http=http, auth_gate=gate)
        with pytest.raises(BitfinexAPIError) as info:
            await client.get_active_funding_credits(ctx=_CTX)
        assert info.value.status_code == 0
        assert not gate.busy
        assert await asyncio.wait_for(client.get_active_funding_credits(ctx=_CTX), 1) == []


@pytest.mark.asyncio
async def test_cancelled_request_releases_the_gate() -> None:
    venue = _Venue()
    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        client = BitfinexAuthREST(http=http, auth_gate=gate)
        venue.hold("funding/credits")
        task = asyncio.ensure_future(client.get_active_funding_credits(ctx=_CTX))
        await asyncio.wait_for(venue.in_flight["funding/credits"].wait(), 1)
        assert gate.busy
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.busy
        assert await asyncio.wait_for(client.get_funding_available_all(ctx=_CTX), 1) == {}


@pytest.mark.asyncio
async def test_venue_error_body_is_logged_without_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=_NONCE_SMALL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BitfinexAuthREST(http=http)
        with caplog.at_level(logging.WARNING), pytest.raises(BitfinexAPIError):
            await client.get_active_funding_credits(ctx=_CTX)
    text = caplog.text
    assert "bitfinex_auth_http_error" in text
    assert "10114" in text and "nonce: small" in text
    assert _KEY not in text and _SECRET not in text


@pytest.mark.asyncio
async def test_error_body_echoing_a_credential_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"bad request: apikey={_KEY}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BitfinexAuthREST(http=http)
        with caplog.at_level(logging.WARNING), pytest.raises(BitfinexAPIError):
            await client.get_key_permissions(ctx=_CTX)
    assert "<redacted:credential_echo>" in caplog.text
    assert _KEY not in caplog.text


@pytest.mark.asyncio
async def test_unshaped_error_body_is_truncated(caplog: pytest.LogCaptureFixture) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>" + "x" * 5000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BitfinexAuthREST(http=http)
        with caplog.at_level(logging.WARNING), pytest.raises(BitfinexAPIError):
            await client.get_funding_available(ctx=_CTX, currency="UST")
    record = next(r for r in caplog.records if "bitfinex_auth_http_error" in r.getMessage())
    assert "<html>" in record.getMessage()
    assert "x" * 300 not in record.getMessage()


def test_trade_row_fixture_matches_parser() -> None:
    from bfx_funding_bot.external.bitfinex.auth_rest import parse_funding_trades

    assert parse_funding_trades(json.loads(json.dumps([_trade_row(1, 5)])))


@pytest.mark.asyncio
async def test_kill_cancel_all_jumps_queued_reads_and_nonces_stay_in_order() -> None:
    """A kill issued while reconcile/history reads are queued goes out right
    after the one in-flight read -- not behind the queue -- and the venue still
    sees strictly increasing nonces."""
    venue = _Venue()
    venue.responses["cancel/all"] = lambda _: _CANCEL_ALL_OK
    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        reads = BitfinexAuthREST(http=http, auth_gate=gate)
        writes = FundingCancelAllClient(http=http, auth_gate=gate)
        release = venue.hold("funding/credits")
        in_flight = asyncio.ensure_future(reads.get_active_funding_credits(ctx=_CTX))
        await asyncio.wait_for(venue.in_flight["funding/credits"].wait(), 1)
        queued = [
            asyncio.ensure_future(reads.get_funding_trades(
                ctx=_CTX, symbol="fUST", start_ms=0, end_ms=10_000)),
            asyncio.ensure_future(reads.get_funding_available_all(ctx=_CTX)),
            asyncio.ensure_future(reads.get_key_permissions(ctx=_CTX)),
        ]
        await _settle()
        assert gate.waiting("read") == 3
        kill = asyncio.ensure_future(writes.cancel_all_funding_offers(currency="UST", ctx=_CTX))
        await _settle()
        assert gate.waiting("order") == 1
        release.set()
        results = await asyncio.gather(in_flight, *queued, kill, return_exceptions=True)
    # permissions parses [] into an empty map; only transport outcomes matter here.
    assert not [r for r in results if isinstance(r, BitfinexAPIError)], results
    assert [p.split("/v2/auth/")[1] for p in venue.accepted] == [
        "r/funding/credits", "w/funding/offer/cancel/all",
        "r/funding/trades/fUST/hist", "r/wallets", "r/permissions",
    ]
    nonces = [n for _, n in venue.arrivals]
    assert nonces == sorted(nonces)
    assert len(set(nonces)) == len(nonces)


@pytest.mark.asyncio
async def test_slow_in_flight_read_is_cut_at_its_deadline_and_the_kill_proceeds() -> None:
    venue = _Venue()
    venue.responses["cancel/all"] = lambda _: _CANCEL_ALL_OK
    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue.handler)) as http:
        reads = BitfinexAuthREST(http=http, auth_gate=gate, read_deadline_s=0.2)
        writes = FundingCancelAllClient(http=http, auth_gate=gate)
        venue.hold("funding/credits")  # never released: a hung venue
        stuck = asyncio.ensure_future(reads.get_active_funding_credits(ctx=_CTX))
        await asyncio.wait_for(venue.in_flight["funding/credits"].wait(), 1)
        loop = asyncio.get_running_loop()
        started = loop.time()
        answer = await asyncio.wait_for(
            writes.cancel_all_funding_offers(currency="UST", ctx=_CTX), 2,
        )
        waited = loop.time() - started
        with pytest.raises(BitfinexAPIError) as info:
            await stuck
    assert answer.outcome == "acknowledged"
    assert 0.15 < waited < 1.0
    assert info.value.status_code == 0
    assert "deadline" in info.value.message
    assert not gate.busy


@pytest.mark.asyncio
async def test_reads_never_overtake_a_waiting_order() -> None:
    gate = AuthRequestGate(iter(range(1, 100)).__next__)
    order_of_entry: list[str] = []

    async def enter(kind: GateKind, name: str) -> None:
        async with gate.nonce(kind, label=name):
            order_of_entry.append(name)
            await asyncio.sleep(0)

    async with gate.nonce("read", label="in-flight"):
        tasks = [asyncio.ensure_future(enter("read", "r1"))]
        await _settle()
        tasks.append(asyncio.ensure_future(enter("order", "o1")))
        await _settle()
        tasks.append(asyncio.ensure_future(enter("read", "r2")))
        tasks.append(asyncio.ensure_future(enter("order", "o2")))
        await _settle()
    await asyncio.gather(*tasks)
    assert order_of_entry == ["o1", "o2", "r1", "r2"]
    assert not gate.busy


@pytest.mark.asyncio
async def test_slow_gate_wait_is_logged_with_the_waiter_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = AuthRequestGate(slow_wait_s=0.05)

    async def kill() -> None:
        async with gate.nonce("order", label="cancel_all"):
            pass

    with caplog.at_level(logging.WARNING):
        async with gate.nonce("read", label="held"):
            waiter = asyncio.ensure_future(kill())
            await asyncio.sleep(0.1)
        await waiter
    record = next(r for r in caplog.records if "bitfinex_auth_gate_slow_wait" in r.getMessage())
    assert "kind=order" in record.getMessage()
    assert "label=cancel_all" in record.getMessage()
