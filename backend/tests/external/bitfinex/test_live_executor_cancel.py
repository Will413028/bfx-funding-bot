"""Unit tests for BitfinexLiveExecutor.cancel — pure fns + I/O shell."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

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
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.strategy import StrategyName
from tests.integration.test_ledger_schema_roles import ledger_db as ledger_db


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
                    http=http, event_sink=sink, bus=DomainEventBus(), phase=Phase.LIVE,
                    strategy=StrategyName.MEAN_REVERSION, configured_symbols=frozenset({"fUST"}),
                    cell="a30", auth_gate=AuthRequestGate(lambda: 123456789),
                ),
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
    from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
    calls = []

    async def transport(request):
        calls.append(request)
        return Response(503)

    async def guard():
        if calls:
            raise CommandGateBlocked("halted during cancel retry")

    context = AccountContext("test", Credentials("mock", "mock"), Decimal("0"))
    context = replace(context, before_cancel_transport=guard)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        executor = _make_executor(http=http, bus=DomainEventBus())
        with pytest.raises(CommandGateBlocked, match="halted during cancel retry"):
            await executor.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                                  account_id="test", ctx=context)
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
    from bfx_funding_bot.modules.execution.errors import InvariantViolation
    with pytest.raises(InvariantViolation):
        classify_cancel_response({"not": "a list"})
    with pytest.raises(InvariantViolation):
        classify_cancel_response([1, 2])  # too short


# ----------------------------------------------------------------------------
# I/O shell tests (Task 9): cancel() REST integration
# ----------------------------------------------------------------------------


def _make_ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="test-key", api_secret="test-secret"),
        allocation_cap_usdt=Decimal("500"),
    )


def _make_executor(
    bus: DomainEventBus,
    http: httpx.AsyncClient,
    base_url: str | None = None,
) -> BitfinexLiveExecutor:
    class _EventCapture:
        async def emit(self, event: dict[str, Any]) -> None:
            return None

    kwargs: dict[str, Any] = {}
    if base_url is not None:
        kwargs["base_url"] = base_url
    return BitfinexLiveExecutor(
        http=http,
        event_sink=_EventCapture(),
        bus=bus,
        phase=Phase.SHADOW,
        strategy=StrategyName.RATE_PERCENTILE,
        configured_symbols=frozenset({"fUSD"}),
        cell="fUSD_p2",
        auth_gate=AuthRequestGate(lambda: 1700000000_000_000),
        date_provider=lambda: date(2026, 5, 23),
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


async def test_cancel_success_publishes_requested_then_acknowledged() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    success_resp = [
        1700000000000, "foc-req", None, None,
        ["123", "fUSD", "rate", "amount"],
        "0", "SUCCESS", "Submitting cancel request",
    ]
    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=success_resp),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )

    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested", "CancelAcknowledged"]
    ack = captured[1]
    assert isinstance(ack, CancelAcknowledged)
    assert ack.rest_status == "success"
    assert ack.venue_offer_id == "123"


async def test_cancel_already_terminal_still_publishes_acknowledged() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    error_resp = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", "Offer not found.",
    ]
    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=error_resp),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )

    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested", "CancelAcknowledged"]
    ack = captured[1]
    assert isinstance(ack, CancelAcknowledged)
    assert ack.rest_status == "already_terminal"


async def test_cancel_auth_error_raises_executor_auth_error() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(401, json={"error": "auth"}),
            )
            with pytest.raises(ExecutorAuthError):
                await executor.cancel(
                    venue_offer_id="123",
                    signal_correlation_id=uuid4(),
                    account_id="default",
                    ctx=_make_ctx(),
                )

    # Intent was published before error
    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested"]


async def test_cancel_5xx_retries_then_swallows(_no_tenacity_sleep: None) -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            route = router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(503, json={"error": "down"}),
            )
            # Should NOT raise — log warn + return
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )
        # transient_retry RETRY_ATTEMPTS=3 → called 3 times
        assert route.call_count == 3

    # Only CancelRequested fired (no Acknowledged)
    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested"]


async def test_cancel_base_url_with_trailing_slash_joins_single_slash() -> None:
    """A trailing slash on base_url must not yield a double-slash request path.

    The path constants carry no leading slash, so naive f-string joining relies
    on base_url having no trailing slash. Normalize defensively instead.
    """
    bus = DomainEventBus()
    success_resp = [
        1700000000000, "foc-req", None, None,
        ["123", "fUSD", "rate", "amount"],
        "0", "SUCCESS", "Submitting cancel request",
    ]
    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http, base_url="https://api.bitfinex.com/")
        with respx.mock(base_url="https://api.bitfinex.com") as router:
            route = router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=success_resp),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )

    assert route.called
    assert route.calls.last.request.url.path == "/v2/auth/w/funding/offer/cancel"


async def test_cancel_other_error_logs_no_acknowledged() -> None:
    bus = DomainEventBus()
    captured: list[Any] = []
    bus.subscribe(CancelRequested, _capture(captured))
    bus.subscribe(CancelAcknowledged, _capture(captured))

    error_resp = [
        1700000000000, "foc-req", None, None, None,
        "0", "ERROR", "Internal error.",
    ]
    async with httpx.AsyncClient() as http:
        executor = _make_executor(bus, http)
        with respx.mock(base_url=BITFINEX_REST_BASE) as router:
            router.post("/v2/auth/w/funding/offer/cancel").mock(
                return_value=Response(200, json=error_resp),
            )
            await executor.cancel(
                venue_offer_id="123",
                signal_correlation_id=uuid4(),
                account_id="default",
                ctx=_make_ctx(),
            )

    # Only CancelRequested fired (no Acknowledged for "other")
    types = [type(e).__name__ for e in captured]
    assert types == ["CancelRequested"]
