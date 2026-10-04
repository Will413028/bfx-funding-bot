"""The seed command's refusals, its digest verification, and two smaller closure cases.

Each refusal leaves every ledger table of the scope empty and the pending requests pending
(the one transaction rolls back). Refusals: missing ``--authorize-seed``; a DSN that is not
the table owner; realm mismatch; epoch already ``ledger``; a runtime session present; the
legacy writer lock held (the legacy process still runs); the ledger not empty (a second
seed); an outcome-less legacy attempt (F6); an uncertainty resolved after the final snapshot
(F5); a credit the ledger cannot key (no venue opening). A tampered row between the expected
digest and the write is a digest mismatch: exit 2, rolled back. The database itself refuses
seed rows from ``bfx_bot`` even if the tool's owner check were gone.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from bfx_funding_bot.apps import ledger_seed as seed_app
from bfx_funding_bot.modules.execution.command_gate import SubmitOutcomeLostError
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import VenueOfferQuarantined
from bfx_funding_bot.modules.execution.ledger_seed import read_seed_closure
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import ResolutionIntent
from bfx_funding_bot.modules.ledger.seed import epoch_rows, expected_digests, plan_seed, write_seed
from bfx_funding_bot.modules.ledger.tables import (
    LEDGER_TABLES,
    AcceptedCapitalBasisQuarantineRow,
    QuarantineMemberRow,
    QuarantineOpeningRow,
)

from .bot_e2e import (
    SCOPE,
    T0,
    AllowOperator,
    BotEnv,
    DyingVenue,
    bot_env,  # noqa: F401 - fixture
    ledger_db,  # noqa: F401 - fixture dependency
    lost_response,
)
from .seed_e2e import (
    acked,
    basis_view,
    boot_as_epoch,
    credit_row,
    flip_epoch,
    latest_bases,
    offer_row,
    run_seed,
    seed_command,
    submit,
    table_count,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LIVE = Decimal("300.00000201")
LOST = Decimal("200.00000202")
FOREIGN = Decimal("75")
SEED_AT = T0 + 100_000


@pytest.fixture
def authority() -> str:
    return "legacy"


async def legacy_with_live_offer(env: BotEnv, *, stop: bool = True) -> Any:
    """Boot, one acknowledged live offer, one accepted snapshot; the process stops."""
    env.venue.wallets = [["funding", "UST", "5000", 0, "5000"]]
    daemon = await env.build()
    await env.boot(daemon, T0)
    env.clock.now = T0 + 10_000
    await submit(env, daemon, LIVE, acked(7101))
    env.venue.offers = [offer_row(7101, LIVE, LIVE, T0 + 10_000, T0 + 10_000)]
    await env.tick(daemon, T0 + 20_000)
    assert daemon.periodic_reconcile._non_accepted == 0
    if stop:
        await stop_process(daemon)
    return daemon


async def stop_process(daemon: Any) -> None:
    await daemon.writer_lock.release()
    daemon.writer_lock = None


async def assert_ledger_empty(env: BotEnv) -> None:
    for table in LEDGER_TABLES:
        assert await table_count(env, table.name) == 0, table.name


async def refused(env: BotEnv, argv: list[str], reason: str, **kwargs: Any) -> dict[str, Any]:
    code, lines = await run_seed(argv, now_ms=SEED_AT, **kwargs)
    assert code == seed_app.EXIT_REFUSED, lines
    summary = lines[-1]
    assert (summary["reason"], summary["committed"]) == (reason, False), summary
    await assert_ledger_empty(env)
    return summary


class Logins:
    """LOGIN roles made for one test on its own database server, dropped afterwards."""

    def __init__(self, url: Any) -> None:
        self.url = url
        self.made: list[str] = []

    def exec(self, sql: str) -> None:
        engine = create_engine(self.url)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(sql)
        finally:
            engine.dispose()

    def member_of(self, role: str) -> str:
        name = "seed_" + uuid4().hex[:12]
        self.made.append(name)
        self.exec(f"CREATE ROLE \"{name}\" LOGIN PASSWORD 'x' NOSUPERUSER")
        self.exec(f'GRANT {role} TO "{name}"')
        return name


@pytest.fixture
def logins(ledger_db: Any) -> Any:  # noqa: F811
    made = Logins(ledger_db.url)
    yield made
    for name in made.made:
        made.exec(f'DROP ROLE IF EXISTS "{name}"')


async def test_guards_refuse_without_authorization_owner_or_matching_realm(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, logins: Logins,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url, authorize=False),
                  "seed_authorization_required")
    runtime = logins.member_of("bfx_bot")
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url.set(username=runtime,
                                                                         password="x")),
                  "dsn_not_owner")
    prod = f"{SCOPE.exchange_account_id}:prod"
    argv = seed_command(env, tmp_path, url=ledger_db.url, realm="prod", run_id="seed-prod",
                        scopes=(prod,))
    manifest = argv[argv.index("--manifest") + 1]
    with open(manifest) as handle:
        body = handle.read().replace(":ci\"", ":prod\"")
    with open(manifest, "w") as handle:
        handle.write(body)
    await refused(env, argv, "realm_mismatch")
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url, run_id="seed-scope",
                                    scopes=(f"{uuid4()}:ci",)), "scope_mismatch")


async def test_the_legacy_process_alive_or_a_runtime_session_refuses(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, logins: Logins,  # noqa: F811
) -> None:
    env = bot_env
    daemon = await legacy_with_live_offer(env, stop=False)
    argv = seed_command(env, tmp_path, url=ledger_db.url)
    await refused(env, argv, "writer_lock_held")  # the daemon's writer lock
    await stop_process(daemon)
    runtime = logins.member_of("bfx_webapi")
    engine = create_async_engine(ledger_db.url.set(drivername="postgresql+asyncpg",
                                                   username=runtime, password="x"))
    try:
        async with engine.connect() as held:
            await held.execute(text("SELECT 1"))
            await refused(env, argv, "runtime_session_present")
    finally:
        await engine.dispose()
    code, lines = await run_seed(argv, now_ms=SEED_AT)
    assert code == 0, lines


async def test_epoch_ledger_and_a_second_seed_refuse(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)
    argv = seed_command(env, tmp_path, url=ledger_db.url)
    code, lines = await run_seed(argv, now_ms=SEED_AT)
    assert code == 0, lines
    code, lines = await run_seed(argv, now_ms=SEED_AT + 1)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "ledger_not_empty")
    await flip_epoch(env, at=SEED_AT + 2)
    code, lines = await run_seed(argv, now_ms=SEED_AT + 3)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "epoch_not_legacy")


async def test_an_epoch_flipped_before_any_seed_refuses(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)
    await flip_epoch(env, at=SEED_AT - 1)
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url), "epoch_not_legacy")


async def test_an_outcome_less_legacy_attempt_refuses(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    daemon = await legacy_with_live_offer(env, stop=False)
    env.clock.now = T0 + 30_000
    from .seed_e2e import ready
    with pytest.raises(SubmitOutcomeLostError):
        await env.submit(daemon, await ready(env, daemon, LOST), DyingVenue(lambda _: None))
    await stop_process(daemon)
    summary = await refused(env, seed_command(env, tmp_path, url=ledger_db.url),
                            "attempt_outcome_missing")
    assert len(summary["detail"]) == 1


async def test_an_uncertainty_resolved_after_the_final_snapshot_refuses(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    daemon = await legacy_with_live_offer(env, stop=False)
    env.clock.now = T0 + 30_000
    await submit(env, daemon, LOST, lost_response())
    await env.tick(daemon, T0 + 40_000)  # the final snapshot holds it unresolved
    (view,) = await env.uncertainties("open")
    env.clock.now = T0 + 50_000
    ref = await env.evidence_ref(view)
    worker = daemon.uncertainty_worker
    worker.authority = AllowOperator()
    async with env.factory.begin() as session:
        queued = await worker.requests.request(
            session, ResolutionIntent(view.uncertainty_id, "mark_not_accepted", ref, "op",
                                      reason="absent"), now_ms=T0 + 50_000)
    assert await worker.tick() is True
    async with env.factory() as session:
        done = await worker.requests.get(session, queued.request_id)
    assert done.state == "applied"
    await stop_process(daemon)
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url), "resolved_after_snapshot")


async def test_a_credit_without_a_venue_opening_refuses(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    env.venue.wallets = [["funding", "UST", "5000", 0, "5000"]]
    row = credit_row(8201, Decimal("100"), T0 - 5_000)
    row[13] = None  # no MTS_OPENING: legacy falls back to MTS_CREATE, the ledger cannot
    env.venue.credits = [row]
    daemon = await env.build()
    await env.boot(daemon, T0)
    assert daemon.periodic_reconcile._non_accepted == 0
    await stop_process(daemon)
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url), "credit_opening_unkeyable")


async def test_a_tampered_row_is_a_digest_mismatch_and_rolls_back(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)

    async def tampered(session: Any, closure: Any, *, now_ms: int) -> Any:
        """Expected digests of the read closure, rows of one changed offer."""
        honest = expected_digests(plan_seed(closure), await epoch_rows(session))
        offer = closure.offers[0]
        changed = replace(closure, offers=(replace(offer, mts_updated=offer.mts_updated + 1),
                                           *closure.offers[1:]))
        result = await write_seed(session, changed, now_ms=now_ms)
        return replace(result, expected=honest)

    monkeypatch.setattr(seed_app, "write_seed", tampered)
    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == seed_app.EXIT_DIGEST, lines
    verification = next(line for line in lines if line["kind"] == "verification")
    assert {m["table"] for m in verification["mismatches"]} == {
        "ledger_observation_offer", "venue_offer_mirror"}
    assert lines[-1]["committed"] is False
    await assert_ledger_empty(env)


async def test_the_database_refuses_seed_rows_from_the_runtime_role(
    bot_env: BotEnv, ledger_db: Any,  # noqa: F811
) -> None:
    """Without the tool's owner check, ``bfx_bot`` still cannot write a seed (owner-only
    seed trigger; the epoch is flipped so the dormancy trigger is not what refuses)."""
    env = bot_env
    await legacy_with_live_offer(env)
    await flip_epoch(env, at=SEED_AT - 1)
    with pytest.raises(Exception, match="ledger seed rows are written by the table owner only"):
        async with env.factory.begin() as session:
            closure = await read_seed_closure(session, SCOPE)
            await session.execute(text("SET LOCAL ROLE bfx_bot"))
            await write_seed(session, closure, now_ms=SEED_AT)
    await assert_ledger_empty(env)


async def test_an_unknown_open_at_the_final_snapshot_is_seeded_unresolved(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """The classification's ``unresolved`` path (not the tail): F8 keeps its uncertainty id."""
    env = bot_env
    daemon = await legacy_with_live_offer(env, stop=False)
    env.clock.now = T0 + 30_000
    await submit(env, daemon, LOST, lost_response())
    await env.tick(daemon, T0 + 40_000)
    await stop_process(daemon)
    async with env.factory() as session:
        (uncertainty,) = await session.scalars(select(ExecutionUncertaintyRow))
    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0, lines
    (seeded,) = await latest_bases(env)
    view = await basis_view(env, seeded.id)
    assert view["attempts"][uncertainty.attempt_id] == "unresolved"
    async with env.factory() as session:
        provenance = await session.scalar(text(
            "SELECT seed_provenance FROM submission_attempt_journal WHERE attempt_id = :a"),
            {"a": uncertainty.attempt_id})
    assert provenance["uncertainty_id"] == str(uncertainty.uncertainty_id)
    assert provenance["tail"] is False


async def test_an_open_legacy_quarantine_keeps_its_id_and_holds_its_offer(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """A pre-D2 quarantine breadcrumb (F8: quarantine_id := uncertainty_id); the ledger's first
    basis keeps it unresolved, and its member is not foreign for conservation."""
    env = bot_env
    daemon = await legacy_with_live_offer(env, stop=False)
    env.venue.offers.append(offer_row(7199, FOREIGN, FOREIGN, T0 + 25_000, T0 + 25_000))
    await env.tick(daemon, T0 + 30_000)
    await EventStorePersister(
        store=PostgresEventStore(deployment_environment="ci"), session_factory=env.factory,
    ).persist(VenueOfferQuarantined(
        venue_offer_id="7199", symbol="fUST", amount=FOREIGN,
        account_id=str(SCOPE.exchange_account_id), observed_at_ms=T0 + 31_000))
    await env.tick(daemon, T0 + 40_000)
    assert daemon.periodic_reconcile._non_accepted == 0
    await stop_process(daemon)
    async with env.factory() as session:
        (uncertainty,) = await session.scalars(select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.state == "open"))
    assert uncertainty.kind == "unattributed_venue_offer"
    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0, lines
    (seeded,) = await latest_bases(env)
    async with env.factory() as session:
        opening = await session.get(QuarantineOpeningRow, uncertainty.uncertainty_id)
        members = list(await session.scalars(select(QuarantineMemberRow)))
        listed = list(await session.scalars(select(AcceptedCapitalBasisQuarantineRow)))
    assert opening is not None and opening.source_attempt_id is None
    assert [(m.source_kind, m.venue_object_id, m.observation_id) for m in members] == [
        ("offer", "7199", seeded.observation_id)]
    assert [row.quarantine_id for row in listed] == [uncertainty.uncertainty_id]

    await flip_epoch(env, at=SEED_AT + 1_000)
    boot_as_epoch(env, monkeypatch)
    ledger = await env.build()
    await env.boot(ledger, SEED_AT + 20_000)
    runner, _ = await latest_bases(env)
    first = await basis_view(env, runner.id)
    assert first["symbols"]["fUST"].conservation == "conserved"
    async with env.factory() as session:
        still = list(await session.scalars(select(AcceptedCapitalBasisQuarantineRow).where(
            AcceptedCapitalBasisQuarantineRow.basis_id == runner.id)))
    assert [row.quarantine_id for row in still] == [uncertainty.uncertainty_id]
    assert await env.has_open(ledger)


async def test_a_pending_request_failure_is_part_of_the_seed(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """The failure is written by the seed transaction, so a refused seed leaves it pending."""
    env = bot_env
    daemon = await legacy_with_live_offer(env, stop=False)
    env.clock.now = T0 + 30_000
    await submit(env, daemon, LOST, lost_response())
    await env.tick(daemon, T0 + 40_000)
    await stop_process(daemon)
    async with env.factory.begin() as session:
        (uncertainty,) = await session.scalars(select(ExecutionUncertaintyRow.uncertainty_id))
        snapshot_seq = await session.scalar(text("SELECT max(event_seq) FROM capital_snapshots"))
        request = UncertaintyResolutionRequestRow(
            request_id=uuid4(), exchange_account_id=SCOPE.exchange_account_id,
            deployment_environment="ci", uncertainty_id=uncertainty, action="mark_not_accepted",
            reconcile_event_seq=snapshot_seq, requested_by="op", created_at_ms=T0 + 45_000)
        session.add(request)
    request_id: UUID = request.request_id
    await refused(env, seed_command(env, tmp_path, url=ledger_db.url, authorize=False),
                  "seed_authorization_required")
    async with env.factory() as session:
        pending = await session.get(UncertaintyResolutionRequestRow, request_id)
    assert pending is not None and pending.state == "requested"
    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0 and lines[0]["failed_requests"] == 1, lines
    async with env.factory() as session:
        failed = await session.get(UncertaintyResolutionRequestRow, request_id)
    assert failed is not None
    assert (failed.state, failed.outcome_reason) == ("failed", "superseded_by_authority_switch")
