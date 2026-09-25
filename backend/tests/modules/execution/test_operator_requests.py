"""The shared operator-request contract (``operator_requests``) on a real outbox table.

Both outboxes (uncertainty adjudication, trading control) inherit this worker;
their own suites cover what applying means. This one pins the contract: one
outcome per request, nothing but the outcome after a refusal, the operator
re-checked when applied, and preparation only outside the transaction.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    NeedsPreparation,
    OperatorRequestWorker,
    Outcome,
    RequestRejected,
    insert_request,
)
from bfx_funding_bot.modules.execution.safety.tables import (
    TradingControlRequestRow,
    TradingStateRow,
)

pytestmark = pytest.mark.integration



class Probe(OperatorRequestWorker[TradingControlRequestRow, str]):
    model = TradingControlRequestRow
    name = "probe"

    def __init__(self, factory: Any, account: UUID, *, behaviour: str = "ok",
                 authorized: bool = True) -> None:
        async def authority(session: Any, *, account_id: UUID, user: str) -> bool:
            return authorized

        super().__init__(session_factory=factory, account_id=account, environment="ci",
                         authority=authority, clock=lambda: 5_000)
        self.behaviour = behaviour
        self.calls: list[str | None] = []
        self.prepared = 0
        self.idled = 0
        self.outcomes: list[tuple[str, str | None]] = []

    async def apply(self, session: Any, row: TradingControlRequestRow, prepared: str | None) -> Outcome:
        self.calls.append(prepared)
        # A write the outcome must not keep unless the request applied.
        session.add(TradingStateRow(
            exchange_account_id=row.exchange_account_id, deployment_environment="ci",
            state="HALTED", cause="operator", actor=row.requested_by,
            reason=f"side effect of {row.request_id}", created_at_ms=1))
        await session.flush()
        if self.behaviour == "reject":
            raise RequestRejected("not_now")
        if self.behaviour == "boom":
            raise RuntimeError("fault")
        if self.behaviour == "prepare" and prepared is None:
            raise NeedsPreparation
        return Outcome(APPLIED, prepared or "done")

    async def prepare(self, row_id: UUID) -> str:
        self.prepared += 1
        return "observed"

    async def idle(self) -> None:
        self.idled += 1

    async def committed(self, row: TradingControlRequestRow, outcome: Outcome) -> None:
        self.outcomes.append((outcome.state, outcome.reason))


@pytest.fixture
async def outbox(migrated_db: Any) -> Any:
    """The migrated PostgreSQL schema: its triggers are the outbox's authority."""
    return migrated_db


def values(account: UUID, **extra: object) -> dict[str, object]:
    return {"request_id": uuid4(), "exchange_account_id": account, "deployment_environment": "ci",
            "action": "resume", "reason": "test",
            "requested_by": "operator", "created_at_ms": 1, **extra}


async def queue(factory: Any, account: UUID) -> UUID:
    request = values(account)
    async with factory.begin() as session:
        assert await insert_request(session, TradingControlRequestRow, request)
    return request["request_id"]  # type: ignore[return-value]


async def row_of(factory: Any, request_id: UUID) -> tuple[str, str | None, int]:
    async with factory() as session:
        row = await session.get(TradingControlRequestRow, request_id)
        writes = len((await session.scalars(select(TradingStateRow))).all())
        return row.state, row.outcome_reason, writes


@pytest.mark.asyncio
async def test_the_web_api_inserts_exactly_the_request_columns_and_one_pending(outbox: Any) -> None:
    factory, account = outbox
    async with factory.begin() as session:
        with pytest.raises(ValueError, match="request values"):
            await insert_request(session, TradingControlRequestRow, values(account, state="applied"))
        assert await insert_request(session, TradingControlRequestRow, values(account))
        assert not await insert_request(session, TradingControlRequestRow, values(account))


@pytest.mark.asyncio
async def test_each_request_gets_exactly_one_outcome(outbox: Any) -> None:
    factory, account = outbox
    request_id = await queue(factory, account)
    worker = Probe(factory, account)
    assert await worker.tick() is True
    assert await worker.process(request_id) == "applied"  # settled: not applied again
    assert worker.calls == [None]
    assert await row_of(factory, request_id) == ("applied", "done", 1)
    # Another account's (or no) request is not this worker's to settle.
    assert await Probe(factory, uuid4()).process(request_id) == "missing"
    assert await worker.process(uuid4()) == "missing"
    assert worker.outcomes == [("applied", "done")]
    assert worker.outcomes == [("applied", "done")]
    # A late failure mark never overwrites the recorded outcome.
    assert await worker._fail(request_id, "late") == "settled"
    assert (await row_of(factory, request_id))[0] == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize(("behaviour", "authorized", "state", "reason"), [
    ("reject", True, "rejected", "not_now"),
    ("boom", True, "failed", "probe_failed:RuntimeError"),
    ("ok", False, "rejected", "operator_not_authorized"),
])
async def test_a_refusal_leaves_nothing_but_the_outcome(outbox: Any, behaviour: str, authorized: bool,
                                                        state: str, reason: str) -> None:
    factory, account = outbox
    request_id = await queue(factory, account)
    worker = Probe(factory, account, behaviour=behaviour, authorized=authorized)
    assert await worker.process(request_id) == state
    assert await row_of(factory, request_id) == (state, reason, 0)
    # The operator is re-checked before anything is applied.
    assert worker.calls == ([] if not authorized else [None])


@pytest.mark.asyncio
async def test_preparation_runs_outside_the_transaction_only_when_asked(outbox: Any) -> None:
    factory, account = outbox
    plain = Probe(factory, account)
    assert await plain.process(await queue(factory, account)) == "applied"
    assert plain.prepared == 0
    prepared = Probe(factory, account, behaviour="prepare")
    request_id = await queue(factory, account)
    assert await prepared.process(request_id) == "applied"
    assert (prepared.prepared, prepared.calls) == (1, [None, "observed"])
    # The first, unprepared attempt was rolled back: one approval, not two.
    assert await row_of(factory, request_id) == ("applied", "observed", 2)


@pytest.mark.asyncio
async def test_housekeeping_runs_only_when_the_queue_is_empty(outbox: Any) -> None:
    factory, account = outbox
    await queue(factory, account)
    worker = Probe(factory, account)
    assert await worker.tick() is True and worker.idled == 0
    assert await worker.tick() is False and worker.idled == 1
