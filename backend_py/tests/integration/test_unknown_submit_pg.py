"""PostgreSQL fault tests for conservative UNKNOWN-submit reconciliation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer, BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import ReservationIntent, ReservationUnknown
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)

pytestmark = pytest.mark.integration

_ENV = "ci"
_ACCOUNT = UUID("00000000-0000-0000-0000-000000000041")


@dataclass(frozen=True)
class _Coverage:
    requested_start_ms: int
    requested_end_ms: int
    oldest_mts_created: int | None
    newest_mts_created: int | None
    pages: int
    complete: bool


@dataclass(frozen=True)
class _History:
    offers: tuple[ActiveFundingOffer, ...]
    coverage: _Coverage


class _Auth:
    def __init__(
        self,
        *,
        active: tuple[ActiveFundingOffer, ...] = (),
        history: _History | Exception | None = None,
    ) -> None:
        self.active = active
        self.history = history or _History(
            (), _Coverage(1_000, 5_000, None, None, 1, True)
        )
        self.calls: list[str] = []

    async def get_active_funding_offers(self, *, ctx, symbol=None):
        del ctx, symbol
        self.calls.append("active")
        return list(self.active)

    async def get_active_funding_credits(self, *, ctx, symbol=None):
        del ctx, symbol
        self.calls.append("credits")
        return []

    async def get_funding_available_all(self, *, ctx):
        del ctx
        self.calls.append("wallets")
        return {"fUST": Decimal("100"), "fUSD": Decimal("50")}

    async def get_funding_offer_history(
        self, *, ctx, start_ms: int, end_ms: int, symbol=None
    ):
        del ctx, symbol
        self.calls.append(f"history:{start_ms}:{end_ms}")
        if isinstance(self.history, Exception):
            raise self.history
        return self.history


class _Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


def _offer(
    venue_offer_id: str,
    *,
    symbol: str = "fUST",
    amount: str = "12.50",
    rate: float | None = 0.00031,
    period: int = 2,
    mts_created: int = 1_500,
    status: str = "ACTIVE",
    offer_type: str | None = "LIMIT",
    flags: dict[str, object] | int | None = 0,
) -> ActiveFundingOffer:
    return ActiveFundingOffer(
        venue_offer_id=venue_offer_id,
        symbol=symbol,
        amount=Decimal(amount),
        amount_original=Decimal(amount),
        rate=cast(float, rate),
        period_days=period,
        mts_created=mts_created,
        mts_updated=mts_created,
        status=status,
        offer_type=offer_type,
        flags=flags,
    )


def _decision(decision_id: str, signal_id: UUID, *, symbol: str) -> ExecutionDecisionRow:
    return ExecutionDecisionRow(
        decision_id=decision_id,
        account_id=str(_ACCOUNT),
        exchange_account_id=_ACCOUNT,
        deployment_environment=_ENV,
        reconcile_id=f"reconcile-{decision_id}",
        cell_id=f"cell-{symbol}",
        symbol=symbol,
        signal_correlation_id=str(signal_id),
        outcome="ready",
        signal_rate=Decimal("0.00031"),
        applied_rate=Decimal("0.00031"),
        amount_usdt=Decimal("12.50"),
        duration_days=2,
        model_evidence={},
        safety_result={},
        execution_policy="book_guarded",
        service_version="test",
        config_hash="config",
        occurred_at_ms=999,
        recorded_at_ms=999,
    )


async def _seed_attempt(pg_session_factory, *, unknown: bool) -> None:
    signal_id = uuid4()
    decision_id = "decision-unknown"
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="unknown"))
        session.add(_decision(decision_id, signal_id, symbol="fUST"))
        await session.commit()
    attempt = SubmissionAttemptPayload(
        execution_decision_id=decision_id,
        account_id=_ACCOUNT,
        environment=_ENV,
        symbol="fUST",
        cid=41,
        normalized_payload={
            "type": "LIMIT",
            "symbol": "fUST",
            "amount": "12.50",
            "rate": "0.00031",
            "period": 2,
            "flags": 0,
        },
        started_at_ms=1_000,
    )
    intent = ReservationIntent(
        symbol="fUST",
        cid=41,
        signal_correlation_id=signal_id,
        account_id=str(_ACCOUNT),
        is_simulated=False,
        execution_decision_id=decision_id,
        amount=Decimal("12.50"),
        occurred_at_ms=1_000,
        submission_attempt=attempt,
    )
    persister = EventStorePersister(
        store=PostgresEventStore(deployment_environment=_ENV),
        session_factory=pg_session_factory,
    )
    await persister.persist(intent)
    if unknown:
        await persister.persist(
            ReservationUnknown(
                symbol="fUST",
                cid=41,
                signal_correlation_id=signal_id,
                account_id=str(_ACCOUNT),
                is_simulated=False,
                reason="transport_response_dropped",
                amount=Decimal("12.50"),
                occurred_at_ms=1_001,
                reservation_ref=intent.reservation_ref,
            )
        )


def _recovery(pg_session_factory, auth: _Auth, bus: _Bus | None = None) -> BootRecovery:
    return BootRecovery(
        store=PostgresEventStore(deployment_environment=_ENV),
        session_factory=pg_session_factory,
        auth_rest=auth,
        account_ctx=AccountContext(
            account_id=str(_ACCOUNT),
            credentials=Credentials(api_key="key", api_secret="secret"),
            allocation_cap_usdt=Decimal("1000"),
        ),
        deployment_environment=_ENV,
        bus=bus or _Bus(),
        symbols=["fUST", "fUSD"],
        grace_ms=100,
        clock=lambda: 5_000,
        uncertainty_handler=lambda _event: _noop(),
    )


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_pending_restart_emits_unknown_then_exact_match_and_resolves(pg_session_factory):
    """Removing the explicit UNKNOWN-before-match stage reopens the retry race."""
    await _seed_attempt(pg_session_factory, unknown=False)
    accepted = _offer("venue-accepted")
    history = _History(
        (accepted,), _Coverage(1_000, 5_000, 1_500, 1_500, 1, True)
    )
    auth = _Auth(active=(accepted, _offer("unknown-symbol", symbol="fXYZ", amount="3")), history=history)
    bus = _Bus()

    result = await _recovery(pg_session_factory, auth, bus).run()

    async with pg_session_factory() as session:
        event_types = (
            await session.execute(
                select(EventLogRow.event_type)
                .where(EventLogRow.exchange_account_id == _ACCOUNT)
                .order_by(EventLogRow.event_seq)
            )
        ).scalars().all()
        attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
        uncertainty = (
            await session.execute(
                select(ExecutionUncertaintyRow).where(
                    ExecutionUncertaintyRow.kind == "submit_outcome_unknown"
                )
            )
        ).scalar_one()
        symbols = set((await session.execute(select(PositionStateRow.symbol))).scalars())
    unknown_index = event_types.index("SUBMIT_OUTCOME_UNKNOWN")
    snapshot_index = event_types.index("VENUE_SNAPSHOT_OBSERVED")
    match_index = event_types.index("SUBMIT_MATCHED_TO_VENUE_OFFER")
    assert unknown_index < snapshot_index < match_index
    assert attempt.outcome_kind == "acknowledged"
    assert attempt.venue_offer_id == "venue-accepted"
    assert uncertainty.state == "resolved"
    assert result.n_unknown == 1
    assert result.n_matched == 1
    assert {"fUST", "fUSD", "fXYZ"} <= symbols
    assert any(call.startswith("history:1000:5000") for call in auth.calls)

    async with pg_session_factory() as session:
        await PostgresEventStore(deployment_environment=_ENV).rebuild_snapshot_from_log(
            session,
            account_id=str(_ACCOUNT),
            deployment_environment=_ENV,
        )
        await session.commit()
    async with pg_session_factory() as session:
        rebuilt_attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
        rebuilt_uncertainty = (
            await session.execute(
                select(ExecutionUncertaintyRow).where(
                    ExecutionUncertaintyRow.kind == "submit_outcome_unknown"
                )
            )
        ).scalar_one()
    assert rebuilt_attempt.venue_offer_id == "venue-accepted"
    assert rebuilt_uncertainty.state == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history", "case"),
    [
        (_History((), _Coverage(1_000, 5_000, None, None, 1, True)), "reject_drop_zero"),
        (
            _History(
                (_offer("candidate-a", status="CANCELED"), _offer("candidate-b", status="EXECUTED")),
                _Coverage(1_000, 5_000, 1_500, 1_500, 1, True),
            ),
            "multiple",
        ),
        (
            _History(
                (_offer("candidate-only", status="CANCELED"),),
                _Coverage(1_000, 5_000, 1_500, 1_500, 1, False),
            ),
            "incomplete",
        ),
        (
            _History(
                (_offer("wrong-decimal", rate=0.0003100000001, status="CANCELED"),),
                _Coverage(1_000, 5_000, 1_500, 1_500, 1, True),
            ),
            "non_exact_decimal",
        ),
        (
            _History(
                (_offer("missing-type", offer_type=None, status="CANCELED"),),
                _Coverage(1_000, 5_000, 1_500, 1_500, 1, True),
            ),
            "missing_type",
        ),
        (
            _History(
                (_offer("missing-flags", flags=None, status="CANCELED"),),
                _Coverage(1_000, 5_000, 1_500, 1_500, 1, True),
            ),
            "missing_flags",
        ),
        (
            _History(
                (_offer("malformed-rate", rate=None, status="CANCELED"),),
                _Coverage(1_000, 5_000, 1_500, 1_500, 1, True),
            ),
            "malformed_rate",
        ),
        (BitfinexShapeError("malformed history"), "malformed"),
        (BitfinexAPIError(status_code=500, message="server error", raw="boom"), "http_5xx"),
    ],
)
async def test_zero_multiple_or_incomplete_evidence_stays_unknown_and_other_symbols_continue(
    pg_session_factory, history, case
):
    """Empty, ambiguous, or unbounded history must never become a rejection."""
    del case
    await _seed_attempt(pg_session_factory, unknown=True)
    bus = _Bus()

    result = await _recovery(pg_session_factory, _Auth(history=history), bus).run()

    async with pg_session_factory() as session:
        attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
        uncertainty = (await session.execute(select(ExecutionUncertaintyRow))).scalar_one()
        match_count = len(
            (
                await session.execute(
                    select(EventLogRow).where(
                        EventLogRow.event_type == "SUBMIT_MATCHED_TO_VENUE_OFFER"
                    )
                )
            ).scalars().all()
        )
    assert attempt.outcome_kind == "unknown"
    assert attempt.venue_offer_id is None
    assert uncertainty.state == "open"
    assert match_count == 0
    assert result.n_matched == 0
    assert any(getattr(event, "symbol", None) == "fUSD" for event in bus.events)


def _wire_offer(offer_id: int, mts_created: int) -> list[object | None]:
    row: list[object | None] = [None] * 21
    row[0] = offer_id
    row[1] = "fUST"
    row[2] = mts_created
    row[3] = mts_created
    row[4] = -12.5
    row[5] = -12.5
    row[6] = "LIMIT"
    row[9] = 0
    row[10] = "CANCELED"
    row[14] = 0.00031
    row[15] = 2
    return row


@pytest.mark.asyncio
async def test_history_transport_pages_backward_and_records_complete_coverage_fence():
    """Changing the cursor/end fence must make pagination coverage observable."""
    bodies: list[dict[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json=[_wire_offer(3, 3_000), _wire_offer(2, 2_000)])
        return httpx.Response(200, json=[_wire_offer(1, 1_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            nonce_provider=iter((1, 2)).__next__,
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
                allocation_cap_usdt=Decimal("1000"),
            ),
            start_ms=1_000,
            end_ms=5_000,
            limit=2,
        )

    assert bodies == [
        {"start": 1_000, "end": 5_000, "limit": 2},
        {"start": 1_000, "end": 1_999, "limit": 2},
    ]
    assert [offer.venue_offer_id for offer in result.offers] == ["1", "2", "3"]
    assert result.coverage.complete is True
    assert result.coverage.oldest_mts_created == 1_000
    assert result.coverage.newest_mts_created == 3_000
    assert result.coverage.pages == 2


@pytest.mark.asyncio
async def test_full_history_page_at_start_fence_remains_incomplete():
    """A full boundary page may hide more rows sharing the oldest timestamp."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_wire_offer(2, 1_000), _wire_offer(1, 1_000)],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            nonce_provider=iter((1,)).__next__,
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
                allocation_cap_usdt=Decimal("1000"),
            ),
            start_ms=1_000,
            end_ms=5_000,
            limit=2,
        )

    assert result.coverage.complete is False
    assert result.coverage.oldest_mts_created == 1_000
    assert result.coverage.pages == 1


@pytest.mark.asyncio
async def test_history_5xx_after_visible_venue_side_effect_stays_one_submit_unknown(
    pg_session_factory,
):
    """An active identity candidate is not an orphan when coverage is incomplete."""
    await _seed_attempt(pg_session_factory, unknown=True)
    auth = _Auth(
        active=(_offer("accepted-but-response-dropped"),),
        history=BitfinexAPIError(status_code=500, message="server error", raw="boom"),
    )

    result = await _recovery(pg_session_factory, auth).run()

    async with pg_session_factory() as session:
        uncertainties = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalars().all()
        quarantine_count = len(
            (
                await session.execute(
                    select(EventLogRow).where(
                        EventLogRow.event_type == "VENUE_OFFER_QUARANTINED"
                    )
                )
            ).scalars().all()
        )
    assert [(row.kind, row.state) for row in uncertainties] == [
        ("submit_outcome_unknown", "open")
    ]
    assert quarantine_count == 0
    assert result.n_matched == 0
    assert result.n_quarantined == 0
