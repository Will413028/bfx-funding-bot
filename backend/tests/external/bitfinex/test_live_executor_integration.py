"""BitfinexLiveExecutor.submit over a fake HTTP transport.

The adapter takes a venue ``OfferRequest`` and the account's credentials and
returns a typed ``SubmitOutcome``; the account command gate owns the reservation
reference (see ``tests/modules/execution/test_command_gate.py``). NOT_SENT is
returned only when no byte can have reached the venue; once the request has
started every ending is classified.
"""
import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.external.bitfinex import live_executor
from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.external.bitfinex.order_port import OfferRequest
from bfx_funding_bot.external.bitfinex.submit_outcomes import (
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcome,
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    SubmitRejected,
)
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload


def _make_decision(*, symbol: str = "fUST") -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0005,
        offer_amount_usdt=150.0,
        offer_duration_days=2,
        symbol=symbol,
    )


def _make_ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
    )


def _ready(decision: DecisionPayload) -> ReadyToSubmit:
    from tests.external.bitfinex.test_funding_rules import evidence
    return ReadyToSubmit(
        decision=decision, decision_id="d-live-test", policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="snapshot-live-test", model_version=None,
        evidence={}, safety=GuardResult(allowed=True, guard_name="test"),
        funding_amount_evidence=evidence(now=int(time.time() * 1000)),
    )


def _offer(ready: ReadyToSubmit) -> OfferRequest:
    """The request the command gate builds for ``ready``."""
    decision = ready.decision
    assert decision.offer_amount_usdt is not None and decision.offer_rate is not None
    assert decision.offer_duration_days is not None
    return OfferRequest(
        symbol=decision.symbol,
        amount=decision.offer_amount_usdt,
        rate=decision.offer_rate,
        period_days=decision.offer_duration_days,
        funding_amount_evidence=ready.funding_amount_evidence,
    )


async def _submit(
    executor: BitfinexLiveExecutor, ready: ReadyToSubmit, ctx: AccountContext, *,
    pre_send: Callable[[], bool] | None = None,
) -> SubmitOutcome:
    return await executor.submit(_offer(ready), ctx, pre_send=pre_send)


def _executor(
    http: httpx.AsyncClient, *, auth_gate: AuthRequestGate | None = None,
    clock: Callable[[], int] | None = None,
) -> BitfinexLiveExecutor:
    return BitfinexLiveExecutor(
        http=http, configured_symbols=frozenset({"fUST"}),
        auth_gate=auth_gate if auth_gate is not None else AuthRequestGate(lambda: 1000),
        clock=clock,
    )


def _gate_is_free(gate: AuthRequestGate) -> bool:
    """Another signed call can take the gate right now: its acquire finishes without yielding."""
    acquiring = gate.acquire("order", label="next")
    try:
        acquiring.send(None)
    except StopIteration:
        gate.release()
        return True
    acquiring.close()
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "changed", "below_minimum", "precision"])
async def test_submit_without_authoritative_amount_rule_is_not_sent(fault):
    from dataclasses import replace
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=SUCCESS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor = BitfinexLiveExecutor(http=http, configured_symbols=frozenset({"fUST"}))
        amount = Decimal({"missing": "150", "changed": "150", "below_minimum": "2",
                           "precision": "150.000000001"}[fault])
        ready = _ready(_make_decision().model_copy(update={"offer_amount_usdt": amount}))
        if fault == "missing":
            ready = replace(ready, funding_amount_evidence=None)
        elif fault == "changed":
            ready = replace(ready, funding_amount_evidence=replace(ready.funding_amount_evidence,
                                                                  rule_digest="old"))
        result = await _submit(executor, ready, _make_ctx())
    assert result.kind is SubmitOutcomeKind.NOT_SENT
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("expiring", ["book", "fx"])
async def test_submit_rechecks_original_book_after_local_signing_work(expiring):
    from dataclasses import replace

    from tests.external.bitfinex.test_funding_rules import evidence

    now = 1100
    requests = []
    def nonce():
        nonlocal now
        now = 3100
        return 1000
    def handler(request):
        requests.append(request)
        return httpx.Response(500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor = _executor(http, auth_gate=AuthRequestGate(nonce), clock=lambda: now)
        ready = replace(_ready(_make_decision()),
                        funding_amount_evidence=evidence(now=-27900 if expiring == "fx" else 1000))
        result = await _submit(executor, ready, _make_ctx(),
                               pre_send=lambda: expiring == "fx" or now <= 2100)
    assert result.kind is SubmitOutcomeKind.NOT_SENT
    assert requests == []


@pytest.mark.asyncio
async def test_submit_returns_submitted_on_success() -> None:
    success_response = [
        1716383500000, "fon-req", None, None,
        [42, "fUSD", 0, 0, 150.0, 0, "REQ", None, None, 0, "ACTIVE",
         None, None, None, 0.0005, 2, 0, 0, None, 0, None, None, None, 12345],
        None, "SUCCESS", "Submitting",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=success_response)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _submit(_executor(http), _ready(_make_decision()), _make_ctx())
    assert isinstance(result, SubmitAcknowledged)
    assert result.venue_offer_id == "42"


@pytest.mark.asyncio
async def test_submit_returns_unknown_on_http_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _submit(_executor(http), _ready(_make_decision()), _make_ctx())
    assert isinstance(result, SubmitOutcomeUnknown)
    assert result.kind is SubmitOutcomeKind.UNKNOWN
    assert result.transport_started is True
    assert result.http_status == 500


@pytest.mark.asyncio
async def test_submit_returns_rejected_on_http_200_error() -> None:
    venue_error = [
        1716383500000, "fon-req", None, None,
        None, None, "ERROR", "Funds insufficient",
    ]
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=venue_error),
    ))

    result = await _submit(_executor(http), _ready(_make_decision()), _make_ctx())

    assert isinstance(result, SubmitRejected)
    assert result.kind is SubmitOutcomeKind.REJECTED


SUCCESS = [
    1716383500000, "fon-req", None, None,
    [42, "fUST", 0, 0, 150.0, 0, "REQ", None, None, 0, "ACTIVE",
     None, None, None, 5.531e-05, 2, 0, 0, None, 0, None, None, None, 1],
    None, "SUCCESS", "Submitting",
]


@pytest.mark.asyncio
async def test_submit_fixed_point_rate_serialization() -> None:
    """Regression (2026-05-26): submit() serialized rate via str(float).
    With a small rate, the posted payload must carry a fixed-point rate
    (not scientific notation)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=SUCCESS)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=5.531e-05, offer_amount_usdt=150.0, offer_duration_days=2,
        symbol="fUST",
    )
    result = await _submit(_executor(http), _ready(decision), _make_ctx())

    assert result.kind is SubmitOutcomeKind.ACKNOWLEDGED
    assert captured["body"]["rate"] == "0.00005531"
    assert "e" not in captured["body"]["rate"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(("planned", "fingerprint", "wire"), [
    (Decimal("200"), 42, "199.99990042"),
    # 17 significant digits: a float round trip would have changed the fingerprint.
    (Decimal("900000001"), 9999, "900000000.99999999"),
])
async def test_planned_amount_reaches_the_venue_body_unchanged(planned, fingerprint, wire) -> None:
    """D3a: the fingerprinted Decimal the planner chooses is byte-for-byte what
    the decision records (as its audit JSON too), what the command gate's
    durable attempt names and what the venue receives. No float in between."""
    from bfx_funding_bot.modules.execution.command_gate import _normalized_venue_payload
    from bfx_funding_bot.modules.execution.deployment.fingerprinted_amount import (
        FINGERPRINT_SPACE,
        choose_fingerprinted_amount,
    )
    from bfx_funding_bot.modules.trading import fingerprint_of

    amount = choose_fingerprinted_amount(
        planned, seed_key="exact-path",
        in_use=frozenset(range(1, FINGERPRINT_SPACE + 1)) - {fingerprint},
        minimum=Decimal("150"), maximum=planned,
    )
    assert amount == Decimal(wire) and fingerprint_of(amount) == fingerprint
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=Decimal("0.00012345"), offer_amount_usdt=amount, offer_duration_days=2,
        symbol="fUST",
    )
    audit = decision.model_dump(mode="json")
    assert audit["offer_amount_usdt"] == wire and audit["offer_rate"] == "0.00012345"
    assert DecisionPayload.model_validate(audit).offer_amount_usdt == amount
    attempt = _normalized_venue_payload(decision)

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor = BitfinexLiveExecutor(http=http, configured_symbols=frozenset({"fUST"}))
        result = await _submit(executor, _ready(decision), _make_ctx())

    assert result.kind is SubmitOutcomeKind.ACKNOWLEDGED
    assert captured["body"]["amount"] == wire
    assert captured["body"]["rate"] == "0.00012345"
    assert captured["body"] == attempt


@pytest.mark.asyncio
async def test_submit_routes_by_decision_symbol_not_constructor() -> None:
    """Task 2: executor must route to the request's symbol, not a fixed constructor symbol."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=SUCCESS)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    decision = _make_decision(symbol="fUST")
    result = await _submit(_executor(http), _ready(decision), _make_ctx())
    assert captured[0]["symbol"] == "fUST" == decision.symbol
    assert result.kind is SubmitOutcomeKind.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_submit_marks_unconfigured_symbol_not_sent() -> None:
    """Local validation cannot be mistaken for a venue rejection."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=SUCCESS)
    ))
    ex = _executor(http, auth_gate=AuthRequestGate(lambda: 1))
    result = await _submit(ex, _ready(_make_decision(symbol="fUSD")), _make_ctx())
    assert isinstance(result, SubmitNotSent)
    assert result.kind is SubmitOutcomeKind.NOT_SENT
    assert result.reason == "symbol_not_configured"


@pytest.mark.asyncio
async def test_submit_marks_malformed_success_response_unknown() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text='{"status":"SUCCESS"}'),
    ))
    ex = _executor(http, auth_gate=AuthRequestGate(lambda: 1))

    result = await _submit(ex, _ready(_make_decision()), _make_ctx())

    assert isinstance(result, SubmitOutcomeUnknown)
    assert result.raw_response_digest is not None
    assert result.http_status == 200


@pytest.mark.asyncio
async def test_submit_failure_keeps_only_bounded_response_evidence() -> None:
    """Venue failures retain correlation evidence without persisting raw bodies."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text='["error",10001,"funding: not enough balance"]')

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _submit(_executor(http), _ready(_make_decision()), _make_ctx())
    assert isinstance(result, SubmitOutcomeUnknown)
    assert result.http_status == 500
    assert result.raw_response_digest is not None
    assert len(result.raw_response_digest) == 64
    assert "not enough balance" not in result.raw_response_digest
    # The structured venue error is the only text kept (bounded, credential-redacted
    # by the gate before it is made durable); there is no raw body field at all.
    assert not hasattr(result, "raw_response")


@pytest.mark.asyncio
async def test_submit_settles_a_recorded_venue_refusal_as_failed() -> None:
    """A 500 carrying a recorded validation refusal is the venue's answer, not an outage."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=(
            '["error",10001,"Invalid offer: not enough UST balance available in deposit wallet"]'
        ))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _submit(_executor(http), _ready(_make_decision()), _make_ctx())
    assert isinstance(result, SubmitRejected)
    assert result.kind is SubmitOutcomeKind.REJECTED
    assert result.raw_response is not None
    assert result.raw_response["status"] == "HTTP_500"
    assert len(result.raw_response["response_digest"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("book_fresh_after_wait", [True, False])
async def test_submit_waits_for_the_shared_gate_and_rechecks_after_it(
    book_fresh_after_wait: bool,
) -> None:
    """A submit queued behind another signed call on the key neither signs nor
    sends until that call is done, and its final book predicate runs after the
    wait -- so a book that went stale while waiting still blocks the send."""
    requests: list[httpx.Request] = []
    checks: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SUCCESS)

    def predicate() -> bool:
        checks.append(book_fresh_after_wait)
        return book_fresh_after_wait

    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor = BitfinexLiveExecutor(
            http=http, configured_symbols=frozenset({"fUST"}), auth_gate=gate,
        )
        async with gate.nonce("read") as held_nonce:
            task = asyncio.ensure_future(_submit(
                executor, _ready(_make_decision()), _make_ctx(), pre_send=predicate))
            for _ in range(20):
                await asyncio.sleep(0)
            assert requests == []
            assert checks == []
        result = await asyncio.wait_for(task, 1)
    assert checks == [book_fresh_after_wait]
    assert not gate.busy
    if book_fresh_after_wait:
        assert len(requests) == 1
        assert int(requests[0].headers["bfx-nonce"]) > held_nonce
        assert result.kind is not SubmitOutcomeKind.NOT_SENT
    else:
        assert requests == []
        assert result.kind is SubmitOutcomeKind.NOT_SENT


@pytest.mark.asyncio
async def test_submit_cancelled_while_waiting_for_the_gate_is_not_sent() -> None:
    """Cancellation during the gate wait is honoured, but carries the fact that
    nothing was sent, so the intent can close as NOT_SENT instead of UNKNOWN."""
    from bfx_funding_bot.external.bitfinex.submit_outcomes import SubmitCancelledNotSent

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SUCCESS)

    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor = BitfinexLiveExecutor(
            http=http, configured_symbols=frozenset({"fUST"}), auth_gate=gate,
        )
        async with gate.nonce("read"):
            task = asyncio.ensure_future(_submit(executor, _ready(_make_decision()), _make_ctx()))
            for _ in range(20):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(SubmitCancelledNotSent) as info:
                await task
        assert task.cancelled()  # still a cancellation to asyncio
        assert info.value.outcome.reason == "cancelled_waiting_for_venue_gate"
        assert requests == []
        assert not gate.busy
        assert gate.waiting("order") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 500])
async def test_submit_returns_unknown_on_a_body_too_deep_to_decode(status: int) -> None:
    # json.loads raises RecursionError (neither TypeError nor ValueError) past ~10000
    # levels; after transport that must be UNKNOWN, not a raise that ends the process.
    deep = b"[" * 20000 + b"0" + b"]" * 20000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=deep)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _submit(_executor(http), _ready(_make_decision()), _make_ctx())
    assert isinstance(result, SubmitOutcomeUnknown)
    assert result.kind is SubmitOutcomeKind.UNKNOWN


# ------------------------------------------------- R4 adapter contract (a2, b, c, d)


def _fail_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**kwargs: object) -> dict[str, str]:
        raise ValueError("cannot sign")
    monkeypatch.setattr(live_executor, "sign_request", refuse)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "reason"), [
    ("symbol_not_configured", "symbol_not_configured"),
    ("signing_failed", "local_validation_failed"),
    ("payload_unbuildable", "local_validation_failed"),
    ("funding_rule_refused", "funding_rule_or_amount_invalid"),
    ("pre_send_false", "decision_book_invalid_or_expired"),
])
async def test_every_adapter_not_sent_path_sends_no_http_and_frees_the_gate(
    path: str, reason: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a2)/(b): each NOT_SENT ending of the adapter sends nothing, and the per-key
    nonce gate it may have taken is free again for the next signed call."""
    from dataclasses import replace

    requests: list[httpx.Request] = []
    pre_send_calls: list[None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SUCCESS)

    def pre_send() -> bool:
        pre_send_calls.append(None)
        return path != "pre_send_false"

    ready = _ready(_make_decision(symbol="fUSD" if path == "symbol_not_configured" else "fUST"))
    if path == "payload_unbuildable":
        # More decimals than the venue keeps: the body cannot be built exactly.
        ready = _ready(_make_decision().model_copy(
            update={"offer_amount_usdt": Decimal("150.000000001")}))
    elif path == "funding_rule_refused":
        ready = replace(ready, funding_amount_evidence=None)
    elif path == "signing_failed":
        _fail_signing(monkeypatch)

    gate = AuthRequestGate(lambda: 1000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _submit(_executor(http, auth_gate=gate), ready, _make_ctx(),
                               pre_send=pre_send)

    assert isinstance(result, SubmitNotSent)
    assert result.reason == reason
    assert requests == []
    # Only the last check runs the gate's predicate; earlier refusals never reach it.
    assert len(pre_send_calls) == (1 if path == "pre_send_false" else 0)
    assert not gate.busy
    assert _gate_is_free(gate)


@pytest.mark.asyncio
async def test_pre_send_false_releases_the_nonce_gate_for_the_next_signed_request() -> None:
    """(b): a stale book refuses the send while the adapter holds the gate; the very
    next signed request takes the gate at once and is the only one that reaches the venue."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SUCCESS)

    gate = AuthRequestGate()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor = _executor(http, auth_gate=gate)
        refused = await _submit(executor, _ready(_make_decision()), _make_ctx(),
                                pre_send=lambda: False)
        assert not gate.busy and gate.waiting("order") == 0
        sent = await _submit(executor, _ready(_make_decision()), _make_ctx(),
                             pre_send=lambda: True)

    assert isinstance(refused, SubmitNotSent)
    assert refused.reason == "decision_book_invalid_or_expired"
    assert isinstance(sent, SubmitAcknowledged)
    assert len(requests) == 1
    assert not gate.busy


def _raise(exc: BaseException) -> Callable[[httpx.Request], Any]:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "reason", "http_status"), [
    (_raise(httpx.ReadTimeout("timed out")), "timeout", None),
    # A protocol error is not a NetworkError: it is still UNKNOWN, under its own reason.
    (_raise(httpx.RemoteProtocolError("server disconnected")), "submit_exception", None),
    (_raise(httpx.ReadError("connection reset")), "connection_error", None),
    (_raise(asyncio.CancelledError()), "cancelled_after_start", None),
    (_raise(RuntimeError("response handling broke")), "submit_exception", None),
    (lambda request: httpx.Response(200, text="not json"), "malformed_response", 200),
    (lambda request: httpx.Response(200, json={"unexpected": "shape"}),
     "unrecognized_response", 200),
], ids=["timeout", "disconnect", "reset", "cancelled", "exception", "parse_error",
        "unrecognized"])
async def test_every_ending_after_transport_started_is_unknown(
    handler: Callable[[httpx.Request], Any], reason: str, http_status: int | None,
) -> None:
    """(c): once the request has started, a timeout, a dropped connection, a
    cancellation or an unparseable answer is UNKNOWN, never NOT_SENT."""
    gate = AuthRequestGate(lambda: 1000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _submit(_executor(http, auth_gate=gate), _ready(_make_decision()),
                               _make_ctx(), pre_send=lambda: True)

    assert isinstance(result, SubmitOutcomeUnknown)
    assert result.transport_started is True
    assert result.reason == reason
    assert result.http_status == http_status
    assert not gate.busy


_RECORDED_REFUSAL = "Invalid offer: not enough UST balance available in deposit wallet"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "body", "kind"), [
    (500, ["error", 10001, _RECORDED_REFUSAL], SubmitOutcomeKind.REJECTED),
    (500, ["error", 10001, "simulated: venue error"], SubmitOutcomeKind.UNKNOWN),
    (502, ["error", 10001, _RECORDED_REFUSAL], SubmitOutcomeKind.UNKNOWN),
    (503, None, SubmitOutcomeKind.UNKNOWN),
], ids=["recorded_refusal", "unrecorded_message", "gateway", "unavailable"])
async def test_a_5xx_is_rejected_only_when_the_refusal_table_names_it(
    status: int, body: object, kind: SubmitOutcomeKind,
) -> None:
    """(d): only a recorded (500, code, message) refusal settles as REJECTED; every
    other 5xx is UNKNOWN and keeps the HTTP status it came with."""
    response = (httpx.Response(status) if body is None
                else httpx.Response(status, text=json.dumps(body)))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))

    result = await _submit(_executor(http), _ready(_make_decision()), _make_ctx())

    assert result.kind is kind
    if kind is SubmitOutcomeKind.UNKNOWN:
        assert isinstance(result, SubmitOutcomeUnknown)
        assert result.reason == "http_5xx"
        assert result.http_status == status
    else:
        assert isinstance(result, SubmitRejected)
        assert result.reason == f"venue_rejected:10001:{_RECORDED_REFUSAL}"


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("symbol", "", "symbol_missing"),
    ("amount", Decimal("0"), "invalid_submit_payload"),
    ("amount", Decimal("-1"), "invalid_submit_payload"),
    ("amount", 150.0, "invalid_submit_payload"),
    ("rate", Decimal("0"), "invalid_submit_payload"),
    ("rate", Decimal("-0.0001"), "invalid_submit_payload"),
    ("rate", 0.0005, "invalid_submit_payload"),
    ("period_days", 0, "invalid_submit_payload"),
    ("period_days", -2, "invalid_submit_payload"),
    ("period_days", True, "invalid_submit_payload"),
    ("period_days", 2.0, "invalid_submit_payload"),
])
def test_an_offer_request_refuses_invalid_terms(field: str, value: Any, reason: str) -> None:
    terms: dict[str, Any] = {
        "symbol": "fUST", "amount": Decimal("150"), "rate": Decimal("0.0005"), "period_days": 2,
    }
    OfferRequest(**terms)  # the control: valid terms build
    terms[field] = value
    with pytest.raises(ValueError, match=reason):
        OfferRequest(**terms)
