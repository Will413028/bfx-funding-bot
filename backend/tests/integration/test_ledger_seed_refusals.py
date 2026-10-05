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
from sqlalchemy import create_engine, event, select, text
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

    async def tampered(session: Any, closure: Any) -> Any:
        """Expected digests of the read closure, rows of one changed offer."""
        honest = expected_digests(plan_seed(closure), await epoch_rows(session))
        offer = closure.offers[0]
        changed = replace(closure, offers=(replace(offer, mts_updated=offer.mts_updated + 1),
                                           *closure.offers[1:]))
        result = await write_seed(session, changed)
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
            await write_seed(session, closure)
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
    boot_as_epoch(env)
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


async def test_the_writers_are_locked_out_before_the_snapshot_starts(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """R1-2: the writer locks and the runtime-session check run outside any transaction, and
    every snapshot read (epoch, closure) inside the one REPEATABLE READ transaction after them;
    a write or request landing between the checks and the snapshot would otherwise be unseen."""
    env = bot_env
    await legacy_with_live_offer(env)
    seen: list[tuple[str, object]] = []

    def connector(plan: Any) -> Any:
        engine = seed_app.connect(plan)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            seen.append((statement, conn.get_execution_options().get("isolation_level")))

        return engine

    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url),
                                 now_ms=SEED_AT, connector=connector)
    assert code == 0, lines

    def first(fragment: str) -> int:
        return next(i for i, (statement, _) in enumerate(seen) if fragment in statement)

    locks, sessions = first("pg_try_advisory_lock"), first("pg_stat_activity")
    requests = first("LOCK TABLE")
    again = next(i for i, (statement, _) in enumerate(seen)
                 if "pg_stat_activity" in statement and i > requests)
    epoch, closure = first("FROM public.capital_authority_epoch"), first("capital_snapshots")
    assert seen[locks][1] == seen[sessions][1] == "AUTOCOMMIT"
    assert seen[requests][1] == seen[again][1] == "REPEATABLE READ"
    assert seen[epoch][1] == seen[closure][1] == "REPEATABLE READ"
    assert locks < sessions < requests < again < epoch < closure
    # Nothing in the transaction reads before the request lock (the lock takes no snapshot).
    begin = next(i for i in range(sessions + 1, len(seen))
                 if seen[i][1] == "REPEATABLE READ")
    assert all(statement.lstrip().upper().startswith("SET LOCAL")
               for statement, _ in seen[begin:requests])
    assert "pg_advisory_unlock_all" in seen[-1][0]


def _trading_control_insert(request_id: UUID) -> Any:
    return text(
        "INSERT INTO trading_control_requests (request_id, exchange_account_id, "
        "deployment_environment, action, reason, requested_by, created_at_ms) "
        "VALUES (:r, :a, 'ci', 'kill', 'during the seed', 'operator', 1)"
    ).bindparams(r=request_id, a=SCOPE.exchange_account_id)


async def test_an_operator_request_waits_for_the_seed_to_end(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """R2-1: the web API inserts requests under no account lock; while the seed transaction
    runs, an insert into a request table waits on the seed's SHARE lock (it times out on
    ``lock_timeout`` with the seed's backend as its blocker), and goes in after the commit."""
    env = bot_env
    await legacy_with_live_offer(env)
    other = create_async_engine(ledger_db.url.set(drivername="postgresql+asyncpg"))
    blocked: list[str] = []
    real = seed_app.verify_snapshot

    async def during_seed(session: Any, plan: Any) -> None:
        seed_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        async with other.connect() as conn:
            await conn.execute(text("SET lock_timeout = '100ms'"))
            pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            try:
                await conn.execute(_trading_control_insert(uuid4()))
            except Exception as exc:  # the insert could not get its lock
                blocked.append(str(exc))
            await conn.rollback()
            async with other.connect() as probe:  # who held it: the seed's backend
                holders = await probe.scalar(text(
                    "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "WHERE c.relname = 'trading_control_requests' AND l.mode = 'ShareLock' "
                    "AND l.granted AND l.pid = :seed"), {"seed": seed_pid})
            assert holders == 1 and pid != seed_pid
        await real(session, plan)

    monkeypatch.setattr(seed_app, "verify_snapshot", during_seed)
    try:
        code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url),
                                     now_ms=SEED_AT)
        assert code == 0, lines
        assert len(blocked) == 1 and "lock timeout" in blocked[0], blocked
        async with other.begin() as conn:  # the seed ended: the same insert goes in
            await conn.execute(text("SET LOCAL lock_timeout = '100ms'"))
            await conn.execute(_trading_control_insert(uuid4()))
    finally:
        await other.dispose()


async def test_lock_table_takes_no_snapshot(ledger_db: Any) -> None:  # noqa: F811
    """The premise of the ordering above, on this PostgreSQL: a REPEATABLE READ transaction
    that has only run ``SET LOCAL`` and ``LOCK TABLE`` still sees a row committed after them;
    its first read fixes the snapshot."""
    url = ledger_db.url.set(drivername="postgresql+asyncpg")
    seeder, writer = create_async_engine(url), create_async_engine(url)
    insert = text("INSERT INTO exchange_accounts (id, venue, label) VALUES (:i, 'bitfinex', 'x')")
    count = text("SELECT count(*) FROM exchange_accounts WHERE id = :i")
    before, after = uuid4(), uuid4()
    try:
        async with seeder.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            await conn.begin()
            await conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            await conn.execute(text(
                "LOCK TABLE public.trading_control_requests IN SHARE MODE"))
            async with writer.begin() as other:
                await other.execute(insert, {"i": before})
            assert await conn.scalar(count, {"i": before}) == 1  # no snapshot until now
            async with writer.begin() as other:
                await other.execute(insert, {"i": after})
            assert await conn.scalar(count, {"i": after}) == 0  # the first read fixed it
            await conn.rollback()
    finally:
        await seeder.dispose()
        await writer.dispose()


async def test_a_refusal_releases_the_writer_and_request_locks(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """R1-2/R2-1 on the refusal path: a refusal inside the transaction (epoch, after both the
    writer locks and the request lock were taken) leaves the writer keys and the request
    tables free. (A refusal before the transaction is followed by a successful seed in
    ``test_the_legacy_process_alive_or_a_runtime_session_refuses``.)"""
    env = bot_env
    await legacy_with_live_offer(env)
    await flip_epoch(env, at=SEED_AT - 1)
    argv = seed_command(env, tmp_path, url=ledger_db.url)
    await refused(env, argv, "epoch_not_legacy")
    other = create_async_engine(ledger_db.url.set(drivername="postgresql+asyncpg"))
    try:
        async with other.connect() as conn:
            await conn.execute(text("SET lock_timeout = '100ms'"))
            for key in seed_app._lock_keys(SCOPE):
                assert await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
                await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            await conn.execute(_trading_control_insert(uuid4()))
            await conn.commit()
            held = await conn.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid <> pg_backend_pid()"))
        assert held == 0
    finally:
        await other.dispose()
