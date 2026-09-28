"""BitfinexLiveExecutor.cancel_all_funding_offers — the kill switch's venue call.

POST /v2/auth/w/funding/offer/cancel/all with {"currency": ...}; the answer is
[MTS, "foc_all-req", null, null, null, null, STATUS, TEXT]. Only the HTTP
transport is replaced.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
)
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.live_executor import (
    BITFINEX_REST_BASE,
    BitfinexLiveExecutor,
    classify_cancel_all_response,
)
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.errors import InvariantViolation
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.strategy import StrategyName

_PATH = "v2/auth/w/funding/offer/cancel/all"
_OK = [1_700_000_000_000, "foc_all-req", None, None, None, None, "SUCCESS", "Cancelled all"]


class _Sink:
    async def emit(self, event: dict[str, Any]) -> None:
        return None


def _executor(handler) -> tuple[BitfinexLiveExecutor, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BitfinexLiveExecutor(
        http=http, event_sink=_Sink(), bus=DomainEventBus(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, configured_symbols=frozenset({"fUST"}),
        cell="fUST_a30", auth_gate=AuthRequestGate(lambda: 1_700_000_000_000_001),
    ), http


_CTX = AccountContext("acct", Credentials("the-key", "the-secret"), Decimal("0"))


@pytest.fixture
def _instant_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio as _asyncio

    async def _instant(_seconds: float) -> None:
        return None
    monkeypatch.setattr(_asyncio, "sleep", _instant)


async def test_signed_request_names_only_the_currency():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_OK)

    executor, http = _executor(handler)
    async with http:
        result = await executor.cancel_all_funding_offers(currency="UST", ctx=_CTX)
    assert (result.outcome, result.venue_status, result.text) == ("acknowledged", "SUCCESS", "Cancelled all")
    [request] = seen
    assert str(request.url) == f"{BITFINEX_REST_BASE}/{_PATH}"
    assert json.loads(request.content) == {"currency": "UST"}
    nonce = request.headers["bfx-nonce"]
    expected = hmac.new(b"the-secret", f"/api/{_PATH}{nonce}".encode() + request.content,
                        hashlib.sha384).hexdigest()
    assert request.headers["bfx-signature"] == expected
    assert request.headers["bfx-apikey"] == "the-key"


async def test_venue_refusal_is_rejected_with_its_own_words():
    executor, http = _executor(lambda request: httpx.Response(
        200, json=[1, "foc_all-req", None, None, None, None, "ERROR", "nothing to cancel"]))
    async with http:
        result = await executor.cancel_all_funding_offers(currency="USD", ctx=_CTX)
    assert (result.outcome, result.venue_status, result.text) == ("rejected", "ERROR", "nothing to cancel")


@pytest.mark.usefixtures("_instant_retry")
async def test_transient_failures_are_retried_because_cancel_all_is_idempotent():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503) if len(attempts) < 3 else httpx.Response(200, json=_OK)

    executor, http = _executor(handler)
    async with http:
        result = await executor.cancel_all_funding_offers(currency="UST", ctx=_CTX)
    assert result.outcome == "acknowledged"
    assert len(attempts) == 3


@pytest.mark.usefixtures("_instant_retry")
@pytest.mark.parametrize(("status", "error"), [
    (401, ExecutorAuthError), (400, ExecutorFatalError), (503, ExecutorTransientError),
])
async def test_non_success_http_raises_typed_errors(status, error):
    executor, http = _executor(lambda request: httpx.Response(status, json=["error", 10100, "x"]))
    async with http:
        with pytest.raises(error):
            await executor.cancel_all_funding_offers(currency="UST", ctx=_CTX)


@pytest.mark.parametrize("currency", ["fUST", "ust", "", "UST;DROP", "U"])
async def test_invalid_currency_never_reaches_the_venue(currency):
    seen: list[httpx.Request] = []
    executor, http = _executor(lambda request: seen.append(request) or httpx.Response(200, json=_OK))
    async with http:
        with pytest.raises(ValueError, match="invalid funding currency"):
            await executor.cancel_all_funding_offers(currency=currency, ctx=_CTX)
    assert seen == []


@pytest.mark.parametrize("raw", [None, {}, [1, "foc_all-req"], "SUCCESS"])
def test_malformed_response_is_not_an_acknowledgement(raw):
    with pytest.raises(InvariantViolation):
        classify_cancel_all_response(raw)
