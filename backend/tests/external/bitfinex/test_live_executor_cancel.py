"""BitfinexLiveExecutor.cancel -- pure classifiers, the I/O shell, and the cancel
audit events the account command gate publishes around it.

The adapter returns a typed ``CancelResult`` and publishes nothing; the gate
publishes ``CancelRequested`` before the venue call and ``CancelAcknowledged``
only for a success or an already-terminal offer. The event tests therefore run
the gate over the real adapter and a fake HTTP transport.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import text

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.live_executor import (
    BITFINEX_REST_BASE,
    BitfinexLiveExecutor,
    classify_cancel_response,
)
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.external.bitfinex.order_port import (
    CancelOutcome,
    CancelResult,
    VenueResponseShapeError,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_gate import (
    AccountCommandGate,
    CommandGateBlocked,
)
from bfx_funding_bot.modules.execution.contracts import GuardResult
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.ledger import CancelAdmitted, CancelProvenance, Scope
from bfx_funding_bot.modules.strategy import DecisionPayload, StrategyName
from tests.integration.test_ledger_schema_roles import ledger_db as ledger_db
from tests.modules.execution.fake_boundary import Recording


async def _plant_unknown(stack, scope: str) -> None:
    """An UNKNOWN submit appearing while the cancel is in flight, in ``scope``."""
    from uuid import UUID

    from tests.integration.contracts.stacks import SCOPE
    from tests.integration.test_ledger_basis import _observation
    from tests.integration.test_ledger_capital_reader import Book
    from tests.pg_templates import DISABLE_REALM_TRIGGERS_SQL

    if scope == "same":
        await stack.unknown("10")
    elif scope == "other_symbol":
        await stack.unknown("10", symbol="fUSD")
    else:
        other = (SCOPE.exchange_account_id, "shadow") if scope == "other_environment" else (
            UUID("00000000-0000-0000-0000-00000000a002"), "ci")
        async with stack.factory.begin() as session:
            if scope == "other_environment":
                # The clone is stamped ``ci``; a write of another realm needs its trigger off.
                await session.execute(text(DISABLE_REALM_TRIGGERS_SQL))
            else:
                await session.execute(text(
                    "INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','other')"),
                    {"id": str(other[0])})
        book = Book(stack.factory, Scope(*other))
        await book.policy("fUST")
        assert await book.accept(
            _observation("1000", usd=True), started=1000, finished=1050, confirmed=1060,
        ) == "accepted"
        await book.attempt("10", outcome="unknown")


@pytest.mark.integration
@pytest.mark.usefixtures("_no_tenacity_sleep")
@pytest.mark.parametrize("state", ["ACTIVE", "HALTED"])
@pytest.mark.parametrize("scope", ["same", "other_symbol", "other_environment", "other_account", "unreadable", "clear"])
async def test_cancel_retry_checks_current_scoped_uncertainty(ledger_db, monkeypatch, scope, state):
    """Every trading state admits the cancel; a new UNKNOWN or an unreadable uncertainty
    read in the offer's own scope still stops the retry before HTTP, and the other scopes
    are the allowed control group. Runs on the ledger: its journal admits the cancel and
    its reader answers the recheck."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.modules.execution.command_boundary import LedgerCommandEffects
    from bfx_funding_bot.modules.execution.command_gate import (
        AccountCommandGate,
        CommandGateBlocked,
    )
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.execution.safety.hard_guards import (
        ManualKillGuard,
        UncertaintyGuard,
    )
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    from bfx_funding_bot.modules.ledger import Authorized, CommandOutcome
    from tests.integration.contracts.stacks import ACCOUNT, NOW, SCOPE, build_stack, seed_account
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink

    await seed_account(ledger_db)
    engine = create_async_engine(
        ledger_db.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg"))
    try:
        stack = build_stack(async_sessionmaker(engine, expire_on_commit=False))
        await stack.policy()
        await stack.snapshot("1000")
        # A managed offer: authorized on the journal and acknowledged, no HTTP.
        async def no_guard(session) -> None:
            return None

        async with stack.factory.begin() as session:
            attempt = await stack.attempt(session, "200")
            assert isinstance(await stack.journal.authorize(
                session, SCOPE, attempt, await stack.token(), now_ms=NOW, locked_guard=no_guard,
            ), Authorized)
        await stack.journal.record_outcome(
            SCOPE, attempt.attempt_id, CommandOutcome("ack", "101", None, NOW, {}))
        halt = TradingStateRepository(
            stack.factory, account_id=ACCOUNT, deployment_environment="ci")
        await halt.transition("ACTIVE", cause="operator", reason="test", actor="test",
                              now_ms=1200)
        if state != "ACTIVE":
            await halt.transition(state, cause="operator", reason="stopped during cancel",
                                  actor="test")
        requests = []

        async def transport(request):
            requests.append(request)
            if len(requests) == 1:
                if scope == "unreadable":
                    async def unavailable(*args, **kwargs):
                        raise RuntimeError("synthetic uncertainty read failure")
                    monkeypatch.setattr(type(stack.uncertainties), "has_open", unavailable)
                    monkeypatch.setattr(type(stack.uncertainties), "list_open", unavailable)
                elif scope != "clear":
                    await _plant_unknown(stack, scope)
                return Response(503)
            return Response(200, json=[0, "foc-req", None, None, None, 0, "SUCCESS", "ok"])

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            sink = _CapturingSink()
            gate = AccountCommandGate(
                BitfinexLiveExecutor(
                    http=http, configured_symbols=frozenset({"fUST"}),
                    auth_gate=AuthRequestGate(lambda: 123456789),
                ),
                bus=DomainEventBus(),
                uncertainty_reader=stack.uncertainties,
                safety_evaluator=SafetyGuardChain(
                    guards=[ManualKillGuard(trading_state=halt), UncertaintyGuard(
                        reader=stack.uncertainties, deployment_environment="ci")],
                    probe=HealthProbe(), diagnostics=sink, phase=Phase.LIVE,
                    strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(ACCOUNT)),
                deployment_environment="ci", clock=lambda: NOW,
                boundary=stack.boundary(LedgerCommandEffects(DomainEventBus())),
                managed_offers=stack.offers,
            )
            ctx = AccountContext(str(ACCOUNT), Credentials("mock", "mock"), Decimal("0"))
            command = gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                                  account_id=str(ACCOUNT), ctx=ctx)
            if scope in {"same", "unreadable"}:
                with pytest.raises(CommandGateBlocked, match="uncertainty"):
                    await asyncio.wait_for(command, timeout=10)
            else:
                await asyncio.wait_for(command, timeout=10)
    finally:
        await engine.dispose()
    # No subsequent HTTP once uncertainty appears; otherwise idempotent retry survives.
    assert len(requests) == (1 if scope in {"same", "unreadable"} else 2)


@pytest.mark.usefixtures("_no_tenacity_sleep")
async def test_cancel_retry_rechecks_halt_before_each_transport():
    calls = []

    async def transport(request):
        calls.append(request)
        return Response(503)

    async def guard():
        if calls:
            raise CommandGateBlocked("halted during cancel retry")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        executor = _make_executor(http=http)
        with pytest.raises(CommandGateBlocked, match="halted during cancel retry"):
            await executor.cancel("101", _make_ctx(), before_transport=guard)
    assert len(calls) == 1


def test_classify_success() -> None:
    raw = [
        1700000000000, "foc-req", None, None,
        ["123", "fUSD", "rate", "amount"],  # offer_array placeholder
        "0",
        "SUCCESS",
        "Submitting cancel request",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "success"
    assert text == "Submitting cancel request"


# Verbatim from docs.bitfinex.com "Cancel Funding Offer" (TEXT is index 7).
DOC_CANCEL_SUCCESS = [
    1568716652310, "foc-req", None, None,
    [604393839, "fUSD", 1568716545000, 1568716545000, 50, 50, "LIMIT", None,
     None, None, "ACTIVE", None, None, None, 0.06, 2, False, None, None,
     False, None],
    None, "SUCCESS", None,
]


def test_classify_documented_cancel_success_has_null_text() -> None:
    assert len(DOC_CANCEL_SUCCESS) == 8
    assert classify_cancel_response(DOC_CANCEL_SUCCESS) == ("success", None)


def test_classify_documented_cancel_error_offer_not_found_is_already_terminal() -> None:
    raw = [*DOC_CANCEL_SUCCESS[:4], None, None, "ERROR", "Offer not found"]
    assert len(raw) == 8
    assert classify_cancel_response(raw) == ("already_terminal", "Offer not found")


def test_classify_already_terminal_not_found() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", "Offer not found.",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "already_terminal"
    assert text == "Offer not found."


def test_classify_already_terminal_not_active() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", "Offer is not active.",
    ]
    status, _text = classify_cancel_response(raw)
    assert status == "already_terminal"


def test_classify_other_error() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", "Internal error.",
    ]
    status, text = classify_cancel_response(raw)
    assert status == "other"
    assert text == "Internal error."


def test_classify_failure_status_treated_as_other() -> None:
    raw = [
        1700000000000, "foc-req", None, None, None,
        "0", "FAILURE", "Unknown failure",
    ]
    status, _text = classify_cancel_response(raw)
    assert status == "other"


def test_classify_malformed_response_raises() -> None:
    with pytest.raises(VenueResponseShapeError):
        classify_cancel_response({"not": "a list"})
    with pytest.raises(VenueResponseShapeError):
        classify_cancel_response([1, 2])  # too short


# ----------------------------------------------------------------------------
# I/O shell tests (Task 9): cancel() REST integration
# ----------------------------------------------------------------------------

_CANCEL_PATH = "/v2/auth/w/funding/offer/cancel"
_SUCCESS = [
    1700000000000, "foc-req", None, None,
    ["123", "fUSD", "rate", "amount"],
    "0", "SUCCESS", "Submitting cancel request",
]
_NOT_FOUND = [1700000000000, "foc-req", None, None, None, "0", "ERROR", "Offer not found."]
_OTHER = [1700000000000, "foc-req", None, None, None, "0", "ERROR", "Internal error."]

ACCOUNT_ID = UUID("6b0f1f6e-3a3f-4f63-9d1e-6a1a2f0c0a11")
ENVIRONMENT = "ci"
PROVENANCE_SIGNAL = UUID("0c2f7a52-8f4e-4a4b-9b9e-2d6f3b1c5e77")


def _make_ctx() -> AccountContext:
    return AccountContext(
        account_id=str(ACCOUNT_ID),
        credentials=Credentials(api_key="test-key", api_secret="test-secret"),
    )


def _make_executor(
    http: httpx.AsyncClient,
    base_url: str | None = None,
) -> BitfinexLiveExecutor:
    kwargs: dict[str, Any] = {}
    if base_url is not None:
        kwargs["base_url"] = base_url
    return BitfinexLiveExecutor(
        http=http,
        configured_symbols=frozenset({"fUSD"}),
        auth_gate=AuthRequestGate(lambda: 1700000000_000_000),
        **kwargs,
    )


def _capture(events: list[Any]) -> Any:
    async def _h(ev: Any) -> None:
        events.append(ev)
    return _h


@pytest.fixture
def _no_tenacity_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch asyncio.sleep to avoid 7s wall-time on retry-exhaustion tests.

    tenacity's _portable_async_sleep lazy-imports asyncio and calls asyncio.sleep
    at runtime, so module-level monkeypatch is sufficient.
    """
    import asyncio as _asyncio

    async def _instant(_seconds: float) -> None:
        return None
    monkeypatch.setattr(_asyncio, "sleep", _instant)


class _Uncertainty:
    """The gate's scoped uncertainty read; counts every recheck."""

    def __init__(self) -> None:
        self.open = False
        self.reads = 0

    async def has_open(self, session: object, scope: Scope, symbol: str) -> bool:
        self.reads += 1
        return self.open

    async def list_open(self, session: object, scope: Scope, symbol: str | None = None) -> tuple[()]:
        return ()


class _CancelEligible:
    """A safety chain that admits every cancel (cancel eligibility is its own test)."""

    async def evaluate(self, decision: DecisionPayload, context: AccountContext) -> GuardResult:
        return GuardResult(True, "chain")

    async def evaluate_cancel(
        self, decision: DecisionPayload, context: AccountContext,
    ) -> GuardResult:
        return GuardResult(True, "chain")


class _CancelHarness:
    """The account command gate over the real adapter, a fake HTTP transport and an
    in-memory boundary that admits the managed offer ``123``."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str | None = None) -> None:
        self.bus = DomainEventBus()
        self.events: list[Any] = []
        self.bus.subscribe(CancelRequested, _capture(self.events))
        self.bus.subscribe(CancelAcknowledged, _capture(self.events))
        self.uncertainty = _Uncertainty()
        recording = Recording(environment=ENVIRONMENT, account_id=ACCOUNT_ID)
        recording.cancel_admission = CancelAdmitted(
            provenance=CancelProvenance(
                venue_offer_id="123", symbol="fUSD", attempt_id=uuid4(), decision_id="d-1",
                cell_id="fUSD_p2", signal_correlation_id=str(PROVENANCE_SIGNAL),
            ),
            amount=Decimal("150"), rate=Decimal("0.0005"), period_days=2,
        )
        self.recording = recording
        ticks = iter(range(1_000, 2_000))
        self.gate = AccountCommandGate(
            _make_executor(http, base_url),
            bus=self.bus,
            uncertainty_reader=self.uncertainty,
            safety_evaluator=_CancelEligible(),
            deployment_environment=ENVIRONMENT,
            boundary=recording.boundary(),
            managed_offers=recording.offers,
            clock=ticks.__next__,
        )

    async def cancel(self) -> None:
        await self.gate.cancel(
            venue_offer_id="123", signal_correlation_id=uuid4(),
            account_id=str(ACCOUNT_ID), ctx=_make_ctx(),
        )

    @property
    def types(self) -> list[str]:
        return [type(e).__name__ for e in self.events]


def _assert_requested(event: Any) -> None:
    assert isinstance(event, CancelRequested)
    assert event.venue_offer_id == "123"
    assert event.account_id == str(ACCOUNT_ID)
    # The identity is the admitted offer's provenance, not what the caller passed.
    assert event.signal_correlation_id == PROVENANCE_SIGNAL
    assert event.requested_at_ms > 0


async def test_cancel_success_publishes_requested_then_acknowledged() -> None:
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post(_CANCEL_PATH).mock(return_value=Response(200, json=_SUCCESS))
            await harness.cancel()

    assert route.call_count == 1
    assert harness.types == ["CancelRequested", "CancelAcknowledged"]
    _assert_requested(harness.events[0])
    ack = harness.events[1]
    assert isinstance(ack, CancelAcknowledged)
    assert ack.rest_status == "success"
    assert ack.venue_offer_id == "123"
    assert ack.signal_correlation_id == PROVENANCE_SIGNAL
    assert ack.account_id == str(ACCOUNT_ID)
    assert ack.venue_response_text == "Submitting cancel request"
    assert ack.acknowledged_at_ms > harness.events[0].requested_at_ms


async def test_cancel_already_terminal_still_publishes_acknowledged() -> None:
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(return_value=Response(200, json=_NOT_FOUND))
            await harness.cancel()

    assert harness.types == ["CancelRequested", "CancelAcknowledged"]
    _assert_requested(harness.events[0])
    ack = harness.events[1]
    assert isinstance(ack, CancelAcknowledged)
    assert ack.rest_status == "already_terminal"
    assert ack.venue_response_text == "Offer not found."


async def test_cancel_auth_error_raises_executor_auth_error() -> None:
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(return_value=Response(401, json={"error": "auth"}))
            with pytest.raises(ExecutorAuthError):
                await harness.cancel()

    # Intent was published before error
    assert harness.types == ["CancelRequested"]
    _assert_requested(harness.events[0])


async def test_cancel_5xx_retries_then_swallows(_no_tenacity_sleep: None) -> None:
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post(_CANCEL_PATH).mock(
                return_value=Response(503, json={"error": "down"}),
            )
            # Should NOT raise — log warn + return
            await harness.cancel()
        # transient_retry RETRY_ATTEMPTS=3 → called 3 times
        assert route.call_count == 3

    # Only CancelRequested fired (no Acknowledged)
    assert harness.types == ["CancelRequested"]


async def test_cancel_fatal_4xx_publishes_no_acknowledged() -> None:
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post(_CANCEL_PATH).mock(return_value=Response(400, json={}))
            await harness.cancel()

    assert route.call_count == 1
    assert harness.types == ["CancelRequested"]


async def test_cancel_base_url_with_trailing_slash_joins_single_slash() -> None:
    """A trailing slash on base_url must not yield a double-slash request path.

    The path constants carry no leading slash, so naive f-string joining relies
    on base_url having no trailing slash. Normalize defensively instead.
    """
    async with httpx.AsyncClient() as http:
        executor = _make_executor(http, base_url="https://api.bitfinex.com/")
        with respx.mock(base_url="https://api.bitfinex.com") as router:
            route = router.post(_CANCEL_PATH).mock(return_value=Response(200, json=_SUCCESS))
            result = await executor.cancel("123", _make_ctx())

    assert route.called
    assert route.calls.last.request.url.path == _CANCEL_PATH
    assert result.outcome is CancelOutcome.SUCCESS


async def test_cancel_other_error_logs_no_acknowledged() -> None:
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(return_value=Response(200, json=_OTHER))
            await harness.cancel()

    # Only CancelRequested fired (no Acknowledged for "other")
    assert harness.types == ["CancelRequested"]


async def test_cancel_blocked_before_transport_publishes_nothing_and_sends_nothing() -> None:
    """The gate's first recheck runs before the intent event: a scope that turned
    uncertain after admission stops the cancel with no event and no HTTP."""
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        harness.uncertainty.open = True
        with respx.mock(base_url=BITFINEX_REST_BASE, assert_all_called=False) as router:
            route = router.post(_CANCEL_PATH).mock(return_value=Response(200, json=_SUCCESS))
            with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
                await harness.cancel()

    assert route.call_count == 0
    assert harness.events == []
    assert harness.recording.cancels_admitted == ["123"]


async def test_cancel_retry_blocked_by_the_gate_recheck_propagates_after_the_intent(
    _no_tenacity_sleep: None,
) -> None:
    """(f) the gate's recheck runs before every attempt (once before the intent event,
    then once per attempt inside the adapter) and what it raises reaches the caller."""
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        calls: list[httpx.Request] = []

        def transport(request: httpx.Request) -> Response:
            calls.append(request)
            harness.uncertainty.open = True  # an UNKNOWN lands while the cancel is in flight
            return Response(503)

        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(side_effect=transport)
            with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
                await harness.cancel()

    assert len(calls) == 1
    assert harness.types == ["CancelRequested"]
    # before the event, before attempt 1, before attempt 2 (which it blocks)
    assert harness.uncertainty.reads == 3


async def test_cancel_rechecks_before_every_attempt_through_the_gate(
    _no_tenacity_sleep: None,
) -> None:
    responses = iter([Response(503), Response(503), Response(200, json=_SUCCESS)])
    async with httpx.AsyncClient() as http:
        harness = _CancelHarness(http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post(_CANCEL_PATH).mock(side_effect=lambda request: next(responses))
            await harness.cancel()

    assert route.call_count == 3
    assert harness.uncertainty.reads == 1 + 3
    assert harness.types == ["CancelRequested", "CancelAcknowledged"]


# ------------------------------------------------- adapter results (no events at all)


@pytest.mark.parametrize(("response", "outcome", "text"), [
    (Response(200, json=_SUCCESS), CancelOutcome.SUCCESS, "Submitting cancel request"),
    (Response(200, json=DOC_CANCEL_SUCCESS), CancelOutcome.SUCCESS, None),
    (Response(200, json=_NOT_FOUND), CancelOutcome.ALREADY_TERMINAL, "Offer not found."),
    (Response(200, json=_OTHER), CancelOutcome.OTHER_ERROR, "Internal error."),
    (Response(503), CancelOutcome.TRANSIENT_EXHAUSTED, None),
    (Response(400, json={}), CancelOutcome.FATAL, None),
], ids=["success", "success_null_text", "already_terminal", "other_error",
        "transient_exhausted", "fatal"])
async def test_the_adapter_returns_each_cancel_ending_as_a_typed_result(
    response: Response, outcome: CancelOutcome, text: str | None,
    _no_tenacity_sleep: None,
) -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(return_value=response)
            result = await _make_executor(http).cancel("123", _make_ctx())

    assert isinstance(result, CancelResult)
    assert result.outcome is outcome
    assert result.outcome.acknowledged is (outcome in (
        CancelOutcome.SUCCESS, CancelOutcome.ALREADY_TERMINAL))
    if outcome in (CancelOutcome.TRANSIENT_EXHAUSTED, CancelOutcome.FATAL):
        assert result.text  # the error, for the log
    else:
        assert result.text == text


async def test_the_adapter_raises_on_a_cancel_answer_that_is_not_a_notification() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(return_value=Response(200, json={"not": "a list"}))
            with pytest.raises(VenueResponseShapeError):
                await _make_executor(http).cancel("123", _make_ctx())


async def test_the_adapter_runs_before_transport_before_every_attempt(
    _no_tenacity_sleep: None,
) -> None:
    order: list[str] = []
    responses = iter([Response(503), Response(503), Response(200, json=_SUCCESS)])

    async def before_transport() -> None:
        order.append("check")

    def transport(request: httpx.Request) -> Response:
        order.append("http")
        return next(responses)

    async with httpx.AsyncClient() as http:
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(side_effect=transport)
            result = await _make_executor(http).cancel(
                "123", _make_ctx(), before_transport=before_transport)

    assert order == ["check", "http"] * 3
    assert result.outcome is CancelOutcome.SUCCESS


async def test_the_adapter_lets_before_transport_errors_through_without_sending() -> None:
    async def blocked() -> None:
        raise CommandGateBlocked("halted")

    async with httpx.AsyncClient() as http:
        with respx.mock(base_url=BITFINEX_REST_BASE, assert_all_called=False) as router:
            route = router.post(_CANCEL_PATH).mock(return_value=Response(200, json=_SUCCESS))
            with pytest.raises(CommandGateBlocked, match="halted"):
                await _make_executor(http).cancel("123", _make_ctx(), before_transport=blocked)

    assert route.call_count == 0


async def test_the_adapter_lets_auth_errors_through() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post(_CANCEL_PATH).mock(return_value=Response(403, json={"error": "auth"}))
            with pytest.raises(ExecutorAuthError):
                await _make_executor(http).cancel("123", _make_ctx())
