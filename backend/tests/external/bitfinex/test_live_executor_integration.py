import json
import time
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.events import CancelRequested
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeKind
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)


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
        allocation_cap_usdt=Decimal("10000"),
    )


def _ready(decision: DecisionPayload) -> ReadyToSubmit:
    from tests.external.bitfinex.test_funding_rules import evidence
    return ReadyToSubmit(
        decision=decision, decision_id="d-live-test", policy=ExecutionPolicy.PAPER,
        market_snapshot_id="snapshot-live-test", model_version=None,
        evidence={}, safety=GuardResult(allowed=True, guard_name="test"),
        funding_amount_evidence=evidence(now=int(time.time() * 1000)),
    )


class _EventCapture:
    async def emit(self, event: dict[str, Any]) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "changed", "below_minimum", "precision"])
async def test_submit_without_authoritative_amount_rule_is_not_sent(fault):
    from dataclasses import replace
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=SUCCESS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor = BitfinexLiveExecutor(http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
            phase=Phase.LIVE, strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1")
        amount = Decimal({"missing": "150", "changed": "150", "below_minimum": "2",
                           "precision": "150.000000001"}[fault])
        ready = _ready(_make_decision().model_copy(update={"offer_amount_usdt": amount}))
        if fault == "missing":
            ready = replace(ready, funding_amount_evidence=None)
        elif fault == "changed":
            ready = replace(ready, funding_amount_evidence=replace(ready.funding_amount_evidence,
                                                                  rule_digest="old"))
        result = await executor.submit(ready, _make_ctx())
    assert result.outcome_kind is SubmitOutcomeKind.NOT_SENT
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
        executor = BitfinexLiveExecutor(http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
            phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="C-1", nonce_provider=nonce, clock=lambda: now)
        ready = replace(_ready(_make_decision()),
                        funding_amount_evidence=evidence(now=-27900 if expiring == "fx" else 1000))
        result = await executor.submit(ready,
            replace(_make_ctx(), before_submit_transport=lambda: expiring == "fx" or now <= 2100))
    assert result.outcome_kind is SubmitOutcomeKind.NOT_SENT
    assert requests == []


@pytest.mark.asyncio
async def test_submit_returns_submitted_on_success() -> None:
    success_response = [
        1716383500000, "fon-req", None, None,
        [42, "fUSD", 0, 0, 150.0, 0, "REQ", None, None, 0, "ACTIVE",
         None, None, None, 0.0005, 2, 0, 0, None, 0, None, None, None, 12345],
        None, "SUCCESS", None, "Submitting",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=success_response)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bus = DomainEventBus()
    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=bus,
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        nonce_provider=lambda: 1000,
        date_provider=lambda: date(2026, 5, 22),
    )

    result = await executor.submit(_ready(_make_decision()), _make_ctx())
    assert result.status == "submitted"
    assert result.venue_offer_id == "42"


@pytest.mark.asyncio
async def test_submit_returns_unknown_on_http_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bus = DomainEventBus()
    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=bus,
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        nonce_provider=lambda: 1000,
        date_provider=lambda: date(2026, 5, 22),
    )
    result = await executor.submit(_ready(_make_decision()), _make_ctx())
    assert result.status == "unknown"
    assert result.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert result.venue_offer_id is None


@pytest.mark.asyncio
async def test_submit_returns_unbound_failure_on_http_200_error() -> None:
    venue_error = [
        1716383500000, "fon-req", None, None,
        None, None, "ERROR", None, "Funds insufficient",
    ]
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=venue_error),
    ))
    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        nonce_provider=lambda: 1000, date_provider=lambda: date(2026, 5, 22),
    )

    result = await executor.submit(_ready(_make_decision()), _make_ctx())

    assert result.status == "failed"  # compatibility view for explicit rejection
    assert result.outcome_kind is SubmitOutcomeKind.REJECTED
    assert result.venue_offer_id is None
    assert result.reservation_ref is not None
    assert result.reservation_ref.venue_offer_id is None


SUCCESS = [
    1716383500000, "fon-req", None, None,
    [42, "fUST", 0, 0, 150.0, 0, "REQ", None, None, 0, "ACTIVE",
     None, None, None, 5.531e-05, 2, 0, 0, None, 0, None, None, None, 1],
    None, "SUCCESS", None, "Submitting",
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
    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        configured_symbols=frozenset({"fUST"}), cell="fUST_a30",
        nonce_provider=lambda: 1000, date_provider=lambda: date(2026, 5, 22),
    )
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=5.531e-05, offer_amount_usdt=150.0, offer_duration_days=2,
        symbol="fUST",
    )
    result = await executor.submit(_ready(decision), _make_ctx())

    assert result.status == "submitted"
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
    from bfx_funding_bot.modules.execution.amount_fingerprint import (
        FINGERPRINT_SPACE,
        choose_fingerprinted_amount,
        fingerprint_of,
    )
    from bfx_funding_bot.modules.execution.command_gate import _normalized_venue_payload

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
        executor = BitfinexLiveExecutor(
            http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
            phase=Phase.LIVE, strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}), cell="fUST_a30",
        )
        result = await executor.submit(_ready(decision), _make_ctx())

    assert result.status == "submitted"
    assert captured["body"]["amount"] == wire
    assert captured["body"]["rate"] == "0.00012345"
    assert captured["body"] == attempt


@pytest.mark.asyncio
async def test_submit_routes_by_decision_symbol_not_constructor() -> None:
    """Task 2: executor must route to decision.symbol, not a fixed constructor symbol."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=SUCCESS)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ex = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        cell="fUST_a30", configured_symbols=frozenset({"fUST"}),
        nonce_provider=lambda: 1000, date_provider=lambda: date(2026, 5, 22),
    )
    decision = _make_decision(symbol="fUST")
    result = await ex.submit(_ready(decision), _make_ctx())
    assert captured[0]["symbol"] == "fUST" == decision.symbol
    assert result.status == "submitted"


@pytest.mark.asyncio
async def test_submit_marks_unconfigured_symbol_not_sent() -> None:
    """Local validation cannot be mistaken for a venue rejection."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=SUCCESS)
    ))
    ex = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUST_a30",
        configured_symbols=frozenset({"fUST"}),
        nonce_provider=lambda: 1, date_provider=lambda: date(2026, 5, 22),
    )
    result = await ex.submit(_ready(_make_decision(symbol="fUSD")), _make_ctx())
    assert result.status == "not_sent"
    assert result.outcome_kind is SubmitOutcomeKind.NOT_SENT
    assert isinstance(result.outcome.reason, str)


@pytest.mark.asyncio
async def test_submit_marks_malformed_success_response_unknown() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text='{"status":"SUCCESS"}'),
    ))
    ex = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUST_a30",
        configured_symbols=frozenset({"fUST"}), nonce_provider=lambda: 1,
        date_provider=lambda: date(2026, 5, 22),
    )

    result = await ex.submit(_ready(_make_decision()), _make_ctx())

    assert result.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert result.status == "unknown"
    assert result.outcome.raw_response_digest is not None


@pytest.mark.asyncio
async def test_submit_failure_keeps_only_bounded_response_evidence() -> None:
    """Venue failures retain correlation evidence without persisting raw bodies."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text='["error",10001,"funding: not enough balance"]')

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        configured_symbols=frozenset({"fUST"}), cell="fUST_a30",
        nonce_provider=lambda: 1000, date_provider=lambda: date(2026, 5, 22),
    )
    result = await executor.submit(_ready(_make_decision()), _make_ctx())
    assert result.status == "unknown"
    assert result.outcome_kind is SubmitOutcomeKind.UNKNOWN
    assert result.venue_offer_id is None
    assert result.raw_response is not None
    assert result.raw_response["http_status"] == 500
    assert len(result.raw_response["response_digest"]) == 64
    assert "not enough balance" not in str(result.raw_response)


@pytest.mark.asyncio
async def test_cancel_publishes_cancel_requested() -> None:
    # Bitfinex cancel SUCCESS shape (10 elements; raw[6]="SUCCESS")
    cancel_success_resp = [
        1700000000000, "foc-req", None, None,
        ["42", "fUSD", "rate", "amount"],
        "0", "SUCCESS", None, "Submitting cancel request",
    ]
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=cancel_success_resp)
    ))
    bus = DomainEventBus(clock=lambda: 5000)
    captured: list = []

    async def capture(ev: CancelRequested) -> None:
        captured.append(ev)
    bus.subscribe(CancelRequested, capture)

    executor = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=bus,
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUST"}), cell="C-1",
        nonce_provider=lambda: 1000,
        date_provider=lambda: date(2026, 5, 22),
    )

    sig_id = uuid4()
    await executor.cancel(
        venue_offer_id="42",
        signal_correlation_id=sig_id,
        account_id="default",
        ctx=_make_ctx(),
    )

    assert len(captured) == 1
    assert captured[0].venue_offer_id == "42"
    assert captured[0].signal_correlation_id == sig_id
    assert captured[0].requested_at_ms > 0
