"""Persistent session/epoch authorization using isolated SQL, never a venue."""
import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from bfx_funding_bot.modules.execution.uncertainty_tables import CanaryCommandPermitRow
from tests.integration.test_capital_repository import capital_db as capital_db
from tests.integration.test_capital_repository import capital_engine as capital_engine
from tests.integration.test_capital_repository import intent


async def prepared(factory, account):
    from bfx_funding_bot.modules.execution.release_session import ReleaseSessions
    from bfx_funding_bot.modules.execution.release_tables import ReleaseSessionRow
    # Imported models are registered after capital_db created its metadata.
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(ReleaseSessionRow.metadata.create_all)
    from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
    store = HaltStateStore(factory, account_id=str(account), deployment_environment="ci")
    # The ceremony's epoch (legacy trading_halt) and the trading state that
    # actually stops new offers: a canary is admitted only while both hold.
    halt = await store.set_halted(True, reason="release", actor="operator", now_ms=1000)
    await TradingStateRepository(factory, account_id=account, deployment_environment="ci").transition(
        "HALTED", cause="operator", reason="release", actor="operator", now_ms=1000)
    repo = ReleaseSessions(account, "ci")
    binding = {"release_digest": "r1", "config_digest": "test", "source_revision": "test", "policies": {"fUST": "p1", "fUSD": "disabled"},
               "schema_head": "test", "projector_version": "test"}
    async with factory.begin() as session:
        row = await repo.request(session, operator="operator", symbol="fUST", cell="a30",
                                 strategy="mean_reversion", max_amount=Decimal("250"),
                                 expires_at_ms=5000, now_ms=1000)
        await repo.prepare(session, row.id, binding=binding, halt_id=halt.id,
                           minimum_amount=Decimal("150"), now_ms=1001)
        await repo.request_action(session, row.id, action="authorize", operator="operator",
                                  expected_revision=1, now_ms=1002)
        await repo.authorize(session, row.id, binding=binding, now_ms=1003)
        return repo, row.id, binding, halt.id


@pytest.mark.asyncio
async def test_exact_decision_consumption_is_durable_before_io_and_never_retries(capital_db):
    factory, account = capital_db
    repo, sid, binding, halt_id = await prepared(factory, account)
    event, decision = intent(account, "150")
    decision.strategy = "mean_reversion"
    async with factory.begin() as session:
        session.add(decision)
    async with factory.begin() as session:
        result = await repo.consume(session, sid, binding=binding, decision=decision,
                                    attempt_id=event.submission_attempt.attempt_id, now_ms=1100)
        assert result.state == "consumed"
    # Simulate crash after commit, before HTTP. A new repository instance cannot retry.
    from bfx_funding_bot.modules.execution.release_session import ReleaseBlocked, ReleaseSessions
    async with factory.begin() as session:
        with pytest.raises(ReleaseBlocked, match="permit_already_consumed"):
            await ReleaseSessions(account, "ci").consume(session, sid, binding=binding,
                decision=decision, attempt_id=uuid4(), now_ms=1101)
    async with factory() as session:
        permit = await session.scalar(select(CanaryCommandPermitRow).where(CanaryCommandPermitRow.halt_id == halt_id))
        assert permit.state == "consumed"
        assert permit.amount_usdt == Decimal("150")
        assert permit.execution_decision_id == decision.decision_id
    assert (await HaltStateStore(factory, account_id=str(account), deployment_environment="ci").current()).halted


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["expired", "release", "policy", "cell", "strategy", "amount", "epoch", "audit_config", "audit_release"])
async def test_stale_or_changed_session_cannot_consume(capital_db, fault):
    factory, account = capital_db
    repo, sid, binding, _ = await prepared(factory, account)
    event, decision = intent(account, "150")
    decision.strategy = "mean_reversion"
    now = 5000 if fault == "expired" else 1100
    if fault == "release":
        binding = {**binding, "release_digest": "r2"}
    if fault == "policy":
        binding = {**binding, "policies": {"fUST": "p2"}}
    if fault == "audit_config":
        decision.config_hash = "unrelated-config"
    if fault == "audit_release":
        decision.service_version = "unrelated-release"
    if fault in {"cell", "strategy", "amount"}:
        setattr(decision, {"cell": "cell_id", "strategy": "strategy", "amount": "amount_usdt"}[fault],
                Decimal("251") if fault == "amount" else "wrong")
    if fault == "epoch":
        store = HaltStateStore(factory, account_id=str(account), deployment_environment="ci")
        await store.set_halted(False, reason="fixture", actor="fixture")
        await store.set_halted(True, reason="new", actor="fixture")
    async with factory.begin() as session:
        session.add(decision)
    from bfx_funding_bot.modules.execution.release_session import ReleaseBlocked
    async with factory.begin() as session:
        with pytest.raises(ReleaseBlocked):
            await repo.consume(session, sid, binding=binding, decision=decision,
                               attempt_id=event.submission_attempt.attempt_id, now_ms=now)


@pytest.mark.asyncio
async def test_second_session_same_epoch_cannot_replenish_permit(capital_db):
    factory, account = capital_db
    first, sid1, binding, epoch1 = await prepared(factory, account)
    second, sid2, _, epoch2 = await prepared(factory, account)
    assert epoch1 == epoch2
    from bfx_funding_bot.modules.execution.release_session import ReleaseBlocked
    for repo, sid in [(first, sid1), (second, sid2)]:
        event, decision = intent(account, "150")
        decision.strategy = "mean_reversion"
        async with factory.begin() as session:
            session.add(decision)
        async with factory.begin() as session:
            if sid == sid1:
                await repo.consume(session, sid, binding=binding, decision=decision,
                                  attempt_id=event.submission_attempt.attempt_id, now_ms=1100)
            else:
                with pytest.raises(ReleaseBlocked, match="permit_already_consumed"):
                    await repo.consume(session, sid, binding=binding, decision=decision,
                                      attempt_id=event.submission_attempt.attempt_id, now_ms=1100)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_halt_reassertions_do_not_create_new_epoch(pg_session_factory):
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    account = uuid4()
    async with pg_session_factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="release-race"))
    store = HaltStateStore(pg_session_factory, account_id=str(account), deployment_environment="ci")
    first = await store.set_halted(True, reason="original", actor="operator")
    states = await asyncio.gather(*(store.set_halted(True, reason="again", actor="worker") for _ in range(8)))
    assert {state.id for state in states} == {first.id}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_sessions_share_one_permit_and_rollback_is_atomic(pg_session_factory):
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.release_session import ReleaseBlocked
    account = uuid4()
    factory = pg_session_factory
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="release-consumption-race"))
    repo, first, binding, epoch = await prepared(factory, account)
    _, second, _, _ = await prepared(factory, account)
    events_decisions = [intent(account, "150", cid=i + 1) for i in range(2)]
    async with factory.begin() as session:
        for _, decision in events_decisions:
            decision.strategy = "mean_reversion"
            session.add(decision)
    with pytest.raises(RuntimeError, match="rollback"):
        async with factory.begin() as session:
            event, decision = events_decisions[0]
            await repo.consume(session, first, binding=binding, decision=decision,
                attempt_id=event.submission_attempt.attempt_id, now_ms=1100)
            raise RuntimeError("fixture rollback")
    async with factory() as session:
        assert (await repo.get(session, first)).state == "authorized"
        assert await session.scalar(select(CanaryCommandPermitRow).where(CanaryCommandPermitRow.halt_id == epoch)) is None

    async def consume(sid, pair):
        event, decision = pair
        try:
            async with factory.begin() as session:
                await repo.consume(session, sid, binding=binding, decision=decision,
                    attempt_id=event.submission_attempt.attempt_id, now_ms=1100)
            return "consumed"
        except ReleaseBlocked as exc:
            return str(exc)
    assert sorted(await asyncio.gather(consume(first, events_decisions[0]), consume(second, events_decisions[1]))) == ["consumed", "permit_already_consumed"]
