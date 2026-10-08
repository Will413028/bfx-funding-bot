"""Operator enable/disable of one currency (lending envelope ADR D4), on migrated PostgreSQL.

The worker, the amendment path, the policy tables and the requests are the real
ones; only the operator authority check and the kill switch are fakes. The
restricted roles and the runtime-write trigger are covered in
``test_capital_policy_request_roles.py``.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from bfx_funding_bot.modules.execution.capital_policy_control import CapitalPolicyRequestWorker
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.safety.tables import (
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.execution.trading_control import TradingControlWorker
from bfx_funding_bot.modules.ledger import PolicyStore, Scope
from bfx_funding_bot.modules.ledger.tables import CapitalPolicyRevisionRow
from bfx_funding_bot.modules.ledger.wiring import build_policy_store, build_scope_lock
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.trading import CapitalPolicy, OfferEnvelope

pytestmark = pytest.mark.integration

T0 = 10_000_000
ENVELOPE = OfferEnvelope(min_period_days=2, max_period_days=2, max_open_offers=6,
                         rate_floor_ratio=Decimal("0.5"), min_rate_apr=Decimal("0.01"))


async def allow(session, *, account_id, user) -> bool:
    return user == "operator"


def repository(account) -> PolicyStore:
    """The ledger's policy store of the scope (the store the worker writes through)."""
    return build_policy_store(Scope(account, "ci"))


def worker(factory, account) -> CapitalPolicyRequestWorker:
    return CapitalPolicyRequestWorker(session_factory=factory, account_id=account,
                                      environment="ci", authority=allow,
                                      policy_store=repository(account),
                                      scope_lock=build_scope_lock(),
                                      clock=lambda: T0)


async def seed(factory, account, symbol="fUST", *, enabled=True, envelope=True) -> None:
    policy = CapitalPolicy(enabled=enabled, max_offer_amount=Decimal("200") if envelope else None,
                           envelope=ENVELOPE if envelope else None)
    async with factory.begin() as session:
        await repository(account).apply_policy(session, symbol=symbol, policy=policy,
                                               expected_revision=0, source={"test": True})


async def request(factory, account, action, *, symbol="fUST", by="operator", at=T0):
    request_id = uuid4()
    async with factory.begin() as session:
        await session.execute(insert(CapitalPolicyRequestRow).values(
            request_id=request_id, exchange_account_id=account, deployment_environment="ci",
            symbol=symbol, action=action, reason=f"{action} for test", requested_by=by,
            created_at_ms=at))
    return request_id


async def row_of(factory, request_id) -> CapitalPolicyRequestRow:
    async with factory() as session:
        row = await session.get(CapitalPolicyRequestRow, request_id)
        assert row is not None
        return row


async def applied(factory, account, symbol="fUST"):
    async with factory() as session:
        return await repository(account).read_applied(session, symbol=symbol)


@pytest.mark.asyncio
async def test_disable_then_enable_append_one_revision_each(migrated_db):
    factory, account = migrated_db
    await seed(factory, account)
    before = await applied(factory, account)
    disable = await request(factory, account, "disable")
    assert await worker(factory, account).process(disable) == "applied"
    after = await applied(factory, account)
    assert (after.revision, after.policy.enabled) == (2, False)
    # Only the flag moved; the envelope and every other term stay as they were.
    assert after.policy.envelope == before.policy.envelope
    assert after.policy.max_offer_amount == before.policy.max_offer_amount
    row = await row_of(factory, disable)
    assert row.state == "applied"
    assert row.outcome_reason == "disabled (revision 2)"
    async with factory() as session:
        written = await session.get(CapitalPolicyRevisionRow, after.revision_id)
    # The revision names the request: typed, and as audit text in its source.
    assert written.operator_request_id == disable
    source = written.source
    assert source["request_id"] == str(disable) and source["requested_by"] == "operator"
    assert source["changes"] == {"enabled": "False"} and "amendment_digest" in source

    enable = await request(factory, account, "enable", at=T0 + 1)
    assert await worker(factory, account).process(enable) == "applied"
    assert (await applied(factory, account)).policy.enabled is True
    assert (await applied(factory, account)).revision == 3


@pytest.mark.asyncio
async def test_asking_for_the_state_in_force_writes_no_revision(migrated_db):
    factory, account = migrated_db
    await seed(factory, account, enabled=False)
    current = await applied(factory, account)
    again = await request(factory, account, "disable")
    assert await worker(factory, account).process(again) == "applied"
    row = await row_of(factory, again)
    assert row.outcome_reason == "unchanged: already disabled"
    assert (await applied(factory, account)).revision == current.revision
    async with factory() as session:  # nothing names a request that changed nothing
        named = (await session.get(CapitalPolicyRevisionRow, current.revision_id)).operator_request_id
    assert named is None


@pytest.mark.asyncio
async def test_enabling_without_an_envelope_is_allowed_and_says_so(migrated_db):
    factory, account = migrated_db
    await seed(factory, account, enabled=False, envelope=False)
    enable = await request(factory, account, "enable")
    assert await worker(factory, account).process(enable) == "applied"
    policy = (await applied(factory, account)).policy
    assert (policy.enabled, policy.envelope) == (True, None)
    assert (await row_of(factory, enable)).outcome_reason.startswith("enabled; envelope unset")


@pytest.mark.asyncio
@pytest.mark.parametrize(("seeded", "symbol", "action", "by", "code"), [
    ("fUST", "fUST", "disable", "someone", "operator_not_authorized"),
    ("fUST", "fBTC", "enable", "operator", "policy_unavailable"),
    # fUSD may hold a policy but never an enabled one (PolicyStore.apply_policy).
    ("fUSD", "fUSD", "enable", "operator", "unsupported_enabled_symbol"),
])
async def test_refusals_leave_the_policy_as_it_was(migrated_db, seeded, symbol, action, by, code):
    factory, account = migrated_db
    await seed(factory, account, seeded, enabled=False, envelope=seeded != "fUSD")
    before = await applied(factory, account, seeded)
    request_id = await request(factory, account, action, symbol=symbol, by=by)
    assert await worker(factory, account).process(request_id) == "rejected"
    row = await row_of(factory, request_id)
    assert row.outcome_reason == code
    assert await applied(factory, account, seeded) == before


@pytest.mark.asyncio
async def test_requests_apply_in_the_order_they_were_asked(migrated_db):
    """A pending enable never blocks a disable (their own slots); the later ask wins."""
    factory, account = migrated_db
    await seed(factory, account)
    first = await request(factory, account, "enable", at=T0)
    second = await request(factory, account, "disable", at=T0 + 1)
    w = worker(factory, account)
    assert await w.tick() is True and await w.tick() is True and await w.tick() is False
    assert (await row_of(factory, first)).outcome_reason == "unchanged: already enabled"
    assert (await row_of(factory, second)).state == "applied"
    assert (await applied(factory, account)).policy.enabled is False


class KillSpy:
    async def engage(self, **kwargs):
        return None


@pytest.mark.asyncio
async def test_a_kill_goes_first_and_rejects_a_waiting_enable_but_not_a_disable(migrated_db):
    factory, account = migrated_db
    await seed(factory, account, "fUST", enabled=False)
    await TradingStateRepository(factory, account_id=account, deployment_environment="ci").transition(
        "ACTIVE", cause="operator", actor="t", reason="setup", now_ms=1)
    enable = await request(factory, account, "enable", at=T0 - 20)
    disable = await request(factory, account, "disable", at=T0 - 10)
    kill_id = uuid4()
    async with factory.begin() as session:
        # The kill has its own table and queue: a waiting toggle never delays it.
        await session.execute(insert(TradingControlRequestRow).values(
            request_id=kill_id, exchange_account_id=account, deployment_environment="ci",
            action="kill", reason="incident", requested_by="operator", created_at_ms=T0))
    control = TradingControlWorker(session_factory=factory, account_id=account, environment="ci",
                                   authority=allow, clock=lambda: T0)
    control.kill_switch = KillSpy()
    assert await control.tick() is True
    async with factory() as session:
        assert (await session.get(TradingControlRequestRow, kill_id)).state == "applied"
    rejected = await row_of(factory, enable)
    assert (rejected.state, rejected.outcome_reason) == ("rejected", "superseded_by_kill")
    # A disable only narrows what trading does after a resume: it still applies.
    assert (await row_of(factory, disable)).state == "requested"
    w = worker(factory, account)
    assert await w.tick() is True and await w.tick() is False
    assert (await row_of(factory, disable)).outcome_reason == "unchanged: already disabled"
    assert (await applied(factory, account)).policy.enabled is False


@pytest.mark.asyncio
async def test_outcomes_alert_the_operator(migrated_db, monkeypatch):
    factory, account = migrated_db
    await seed(factory, account)
    sent: list[tuple[str, str | None, dict]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, *, level=None, **fields:
                        sent.append((event, level, fields)))
    w = worker(factory, account)
    assert await w.process(await request(factory, account, "disable")) == "applied"
    assert await w.process(await request(factory, account, "enable", by="someone")) == "rejected"
    assert [(e, lv, f["symbol"], f["action"]) for e, lv, f in sent] == [
        ("capital_policy_request_applied", "warning", "fUST", "disable"),
        ("capital_policy_request_rejected", "warning", "fUST", "enable"),
    ]


@pytest.mark.asyncio
async def test_nothing_but_the_policy_is_written(migrated_db):
    """No trading-state row: disabling is not a halt, and enabling is not a resume."""
    factory, account = migrated_db
    await seed(factory, account)
    assert await worker(factory, account).process(await request(factory, account, "disable")) == "applied"
    async with factory() as session:
        assert (await session.scalars(select(TradingStateRow))).all() == []
