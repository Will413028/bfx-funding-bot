from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.release_session import ReleaseSessions
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from tests.external.bitfinex.test_funding_rules import FixedRules
from tests.integration.test_capital_repository import capital_db as capital_db
from tests.integration.test_capital_repository import capital_engine as capital_engine
from tests.integration.test_capital_repository import repository, setup_policy, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("max_amount,rate,expected,fault", [("200", "0.5", "blocked", None),
    ("400", "0.5", "prepared", None), ("2000", "0.1", "blocked", None),
    ("400", "0.5", "blocked", "delay"), ("400", "0.5", "blocked", "missing"),
    ("400", "0.5", "blocked", "changed")])
async def test_preview_uses_fx_minimum_and_respects_max_amount(capital_db, max_amount, rate, expected, fault):
    from bfx_funding_bot.modules.execution.release_worker import (
        ReleaseCommandAuthority,
        ReleaseWorker,
    )
    from tests.external.bitfinex.test_funding_rules import FixedRules
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo, reserve="0", fraction="0.70")
    await snapshot(factory, repo)
    capital = CapitalRuntime(repository=repo, session_factory=factory, clock=lambda: 1100)
    sessions = ReleaseSessions(account, "ci")
    halt = HaltStateStore(factory, account_id=str(account), deployment_environment="ci")
    await halt.set_halted(True, reason="fixture", actor="operator")
    now = 1100
    async def yes(*args):
        return True
    async def preflight(*args):
        nonlocal now
        if fault == "delay":
            now = 32000
    async def binding(session):
        return {"fixture": "versioned-rule"}
    authority = ReleaseCommandAuthority(repo=sessions, capital=capital, binding_reader=binding,
        ownership=yes, authority_reader=yes, preflight=preflight, clock=lambda: now)
    worker = ReleaseWorker(authority=authority, halt_store=halt, funding_rules=FixedRules(),
        configured_cells=(("mean_reversion", "fUST", "a30"),), halt_authorization=object(),
        planner=yes, observation=yes)
    worker.funding_rules = FixedRules(rate=rate, clock=lambda: 1100)
    async with factory.begin() as session:
        row = await sessions.request(session, operator="operator", symbol="fUST", cell="a30",
            strategy="mean_reversion", max_amount=Decimal(max_amount), expires_at_ms=5000, now_ms=1000)
        sid = row.id
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    active = set()
    def begin(session, transaction, connection):
        active.add(id(session))
    def end(session, transaction):
        if transaction.parent is None:
            active.discard(id(session))
    observe = worker.funding_rules.observe
    async def outside_transaction(symbol):
        assert not active, "FX request must not run inside any account DB transaction"
        proof = await observe(symbol)
        if fault == "changed":
            from dataclasses import replace
            proof = replace(proof, rule_digest="old-rule")
        return proof
    worker.funding_rules.observe = outside_transaction
    if fault == "missing":
        worker.funding_rules = None
    event.listen(Session, "after_begin", begin)
    event.listen(Session, "after_transaction_end", end)
    try:
        await worker.tick()
    finally:
        event.remove(Session, "after_begin", begin)
        event.remove(Session, "after_transaction_end", end)
    async with factory() as session:
        row = await sessions.get(session, sid)
        assert row.state == expected
        if expected == "prepared":
            assert row.minimum_amount == Decimal("300")


@pytest.mark.asyncio
@pytest.mark.parametrize("halt_failure", [False, True])
async def test_worker_prepares_then_rechecks_revoked_authorization(capital_db, halt_failure):
    from bfx_funding_bot.modules.execution.release_worker import (
        ReleaseCommandAuthority,
        ReleaseWorker,
    )
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo, reserve="0", fraction="0.70")
    await snapshot(factory, repo)
    capital = CapitalRuntime(repository=repo, session_factory=factory, clock=lambda: 1100)
    sessions = ReleaseSessions(account, "ci")
    halt = HaltStateStore(factory, account_id=str(account), deployment_environment="ci")
    epoch = await halt.set_halted(True, reason="fixture", actor="operator")
    authorized = True

    async def ownership():
        return True

    async def operator(session, user):
        return authorized

    async def binding(session):
        return {"release_digest": "measured-fixture", "policies": {"fUST": "fixture"}}

    async def preflight(session, row):
        return None

    calls = []
    async def planner(command):
        calls.append(command)

    authority = ReleaseCommandAuthority(repo=sessions, capital=capital, binding_reader=binding,
        ownership=ownership, authority_reader=operator, preflight=preflight, clock=lambda: 1100)
    worker = ReleaseWorker(authority=authority, halt_store=halt, funding_rules=FixedRules(),
        configured_cells=(("mean_reversion", "fUST", "a30"),), halt_authorization=object(),
        planner=planner, observation=preflight)
    async with factory.begin() as session:
        row = await sessions.request(session, operator="operator", symbol="fUST", cell="a30",
            strategy="mean_reversion", max_amount=Decimal("200"), expires_at_ms=5000, now_ms=1000)
        sid = row.id
    await worker.tick()
    async with factory.begin() as session:
        row = await sessions.get(session, sid)
        assert row.state == "prepared"
        assert row.evidence["preparation"]["available_amount"] == "1000"
        assert row.evidence["preparation"]["max_new_offer"] == "700.00"
        assert row.binding["release_digest"] == "measured-fixture"
        assert row.halt_id == epoch.id
        await sessions.request_action(session, sid, action="authorize", operator="operator",
                                      expected_revision=1, now_ms=1100)
    if halt_failure:
        from unittest.mock import AsyncMock
        # Simulate a successful planner that returns after its one-shot call.
        # A failed terminal halt write must NOT be retried and then swallowed.
        halt.set_halted = AsyncMock(side_effect=[OSError("fixture halt failure"), epoch])
        with pytest.raises(Exception, match="halt"):
            await worker.tick()
        assert halt.set_halted.await_count == 1
        return
    authorized = False
    await worker.tick()
    async with factory() as session:
        row = await sessions.get(session, sid)
        assert row.state == "blocked"
        assert row.reason == "release_operator_revoked"
    assert calls == []
    assert (await halt.current()).id == epoch.id


@pytest.mark.asyncio
async def test_production_worker_rejects_unprotected_halt2_source(tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4

    from bfx_funding_bot.core.release_identity import ReleaseIdentityError
    from bfx_funding_bot.modules.execution.release_worker import build_release_worker
    path = tmp_path / "unprotected.json"
    path.write_text("{}")
    async def owned():
        return True
    async def planner(command):
        pytest.fail("readiness must never submit")
    worker = build_release_worker(
        runtime=SimpleNamespace(verify=lambda: SimpleNamespace(actual_image_id="sha256:" + "a"*64)),
        capital=SimpleNamespace(repository=SimpleNamespace(account_id=uuid4(), environment="ci")),
        writer_lock=SimpleNamespace(verify_held=owned), halt_store=None, funding_rules=FixedRules(),
        configured_cells=(("mean_reversion", "fUST", "a30"),), halt_authorization=object(),
        planner=planner, config_artifact=path, evidence_path=path, clock=lambda: 1100)
    row = SimpleNamespace(symbol="fUST", cell="a30", strategy="mean_reversion", max_amount=Decimal("200"))
    with pytest.raises(ReleaseIdentityError, match=r"unprotected|writable"):
        await worker.authority.preflight(None, row)


async def test_worker_binds_halt2_to_actual_host_id_not_source_config(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from bfx_funding_bot.modules.execution import release_worker as module
    from scripts import halt2_cutover, run_canary_preflight
    source_config = "sha256:" + "a"*64
    actual_manifest = "sha256:" + "b"*64
    proof = SimpleNamespace(actual_image_id=actual_manifest,
        manifest=SimpleNamespace(docker_image_id=source_config, environment={}))
    monkeypatch.setattr(module, "assert_protected_file", lambda _: None)
    monkeypatch.setattr(halt2_cutover, "_load_evidence", lambda _: object())
    async def preflight(**kwargs):
        return kwargs["image_digest"]
    monkeypatch.setattr(run_canary_preflight, "verify_release_preflight", preflight)
    async def owned():
        return True
    async def planner(command):
        pytest.fail("preflight must never submit")
    worker = module.build_release_worker(
        runtime=SimpleNamespace(verify=lambda: proof),
        capital=SimpleNamespace(repository=SimpleNamespace(account_id=uuid4(), environment="ci")),
        writer_lock=SimpleNamespace(verify_held=owned), halt_store=None, funding_rules=FixedRules(),
        configured_cells=(("mean_reversion", "fUST", "a30"),), halt_authorization=object(),
        planner=planner, config_artifact=tmp_path / "config", evidence_path=tmp_path / "evidence",
        clock=lambda: 1100)
    row = SimpleNamespace(symbol="fUST", cell="a30", strategy="mean_reversion",
                          max_amount=Decimal("200"), minimum_amount=Decimal("150"))
    assert await worker.authority.preflight(None, row) == actual_manifest
