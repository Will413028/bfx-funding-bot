"""v2 atomicity and release handoff on disposable PostgreSQL, never production."""

import base64
import hashlib
import importlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.projection_cutover.codec import (
    decode_row,
    encode_row,
    row_digest,
)
from bfx_funding_bot.modules.execution.projection_cutover.manifest import (
    TABLE_NAMES,
    decode_manifest,
)
from scripts import cutover_projection as cli
from scripts.projection_cutover_operations import (
    verify_database_quiescence,
    verify_local_operations,
)
from scripts.verify_projection_archive import verify_restore_archives
from tests.integration.test_projection_cutover_archive import archive_db as archive_db
from tests.integration.test_projection_cutover_archive import archive_pg as archive_pg
from tests.scripts.test_cutover_projection import _prepare_kwargs, prepare_fixture

pytestmark = pytest.mark.integration


def runtime():
    return importlib.import_module("bfx_funding_bot.modules.execution.projection_cutover.apply")


async def fixture(archive_db, tmp_path, *, history=False):
    if history:
        from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
        from tests.modules.execution.projection_cutover.test_snapshot import snapshot
        async with archive_db[0].begin() as session:
            await PostgresEventStore(deployment_environment="ci").append_snapshot(session, snapshot())
    factory, kwargs = await prepare_fixture(archive_db)
    kwargs["operation_inventory"]["runtime_roles"] = dict.fromkeys(
        ("bot", "webapi", "frontend", "weekly-report"), kwargs["runtime_roles"][0],
    )
    prepared = await cli.prepare_archive(
        factory, **_prepare_kwargs(kwargs), output=tmp_path / "prepared", dry_run=False,
    )
    payload = (tmp_path / "prepared").read_bytes()
    expected = decode_manifest(encode_row(prepared["manifest"]))
    raw = json.dumps({"schema_version": 2, "target_run_id": str(expected.run_id),
        "prepared": [{"sha256": hashlib.sha256(payload).hexdigest(),
                      "payload": base64.b64encode(payload).decode()}]}).encode()
    async with factory() as session:
        report = await verify_restore_archives(session, scope=expected.scope, raw=raw, archive_only=True)
    receipt = {
        "schema_version": 2, "kind": "archive_restore", "measured": True,
        "target_run_id": str(expected.run_id), "archive_verification": report,
        "restore_run_id": "synthetic-restore", "archive_input_digest": hashlib.sha256(raw).hexdigest(),
        "target_backup_label": "20260910-010000F", "target_time": None,
        "config_digest": "b" * 64, "observed_at_ms": 1200, "rto_seconds": 10,
        "verifier_image_digest": expected.image_digest, "image_digest": "sha256:" + "c" * 64,
        "image_labels": {}, "server_version_num": 180000,
        "migration_heads": list(expected.migration_heads), "event_count": expected.stream.count,
        "event_head": expected.stream.head or None, "event_hash": expected.stream.digest,
        "account_id": str(expected.scope.account_id), "environment": expected.scope.environment,
        "projector_version": expected.projector_version, "network_internal": True,
        "egress_disconnected": True, "verifier_exit_status": 0, "network_name": "bfx-dr-synthetic",
    }

    async def quiescence(session, *, scope, runtime_roles, operation_digest):
        assert scope == expected.scope
        assert runtime_roles == kwargs["runtime_roles"]
        assert operation_digest == row_digest(kwargs["operation_inventory"])
        assert await session.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory')"
        ))
        await verify_database_quiescence(session, scope=scope, runtime_roles=runtime_roles)
        await verify_local_operations(kwargs["operation_inventory"], runner=kwargs["runner"])

    snapshot = replace(kwargs["snapshot"], event_id=UUID(int=777), query_started_at_ms=1300,
                       query_finished_at_ms=1400, occurred_at_ms=1400)
    kwargs["archive_input"] = raw
    return factory, {"expected": expected, "snapshot": snapshot, "archive_restore_receipt": receipt,
        "evidence": kwargs["evidence"], "runtime_roles": kwargs["runtime_roles"],
        "operation_digest": row_digest(kwargs["operation_inventory"]),
        "quiescence_verifier": quiescence, "now_ms": 1500}, kwargs


async def complete_raw(factory):
    async with factory() as session:
        return {name: (await session.scalars(text(
            f"SELECT row_to_json(t)::text FROM {name} t ORDER BY row_to_json(t)::text"
        ))).all() for name in [*("public." + n for n in (*TABLE_NAMES, "event_log", "trading_halt")),
                              "projection_audit.runs", "projection_audit.rows", "projection_audit.receipts"]}


async def test_caller_transaction_commit_and_ambiguous_restart(archive_db, tmp_path):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    before = await complete_raw(factory)
    async with factory() as session:
        with pytest.raises(ValueError, match="transaction"):
            await module.apply_cutover(session, **args)
        assert not session.in_transaction()
        async with session.begin():
            transaction = session.get_transaction()
            receipt = await module.apply_cutover(session, **args)
            assert session.get_transaction() is transaction and transaction.is_active
        # Model an acknowledgement lost after the commit succeeded.
    committed = await complete_raw(factory)
    assert len(committed["public.event_log"]) == 1
    assert len(committed["projection_audit.receipts"]) == 1
    assert len(committed["projection_audit.runs"]) == 2
    assert set(before["projection_audit.rows"]) <= set(committed["projection_audit.rows"])
    assert committed["public.trading_halt"] == before["public.trading_halt"]
    assert receipt["snapshot_event_id"] == args["snapshot"].event_id
    async with factory.begin() as session:
        assert await module.apply_cutover(session, **{**args, "now_ms": 999999999}) == receipt
    assert await complete_raw(factory) == committed
    with pytest.raises(RuntimeError, match="post_commit"):
        raise RuntimeError("post_commit")
    assert await complete_raw(factory) == committed


async def test_fust_only_cutover_rebuilds_only_selected_symbol(archive_db, tmp_path):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    args["snapshot"] = replace(
        args["snapshot"],
        wallet_available={"fUST": Decimal("17")},
    )
    args["managed_symbols"] = frozenset({"fUST"})

    async with factory.begin() as session:
        await module.apply_cutover(session, **args)

    async with factory() as session:
        rows = (await session.execute(text(
            "SELECT symbol, offered_amount, lent_amount, available_amount, uncertain_amount "
            "FROM position_state WHERE exchange_account_id=:account "
            "AND deployment_environment='ci' ORDER BY symbol"
        ), {"account": args["expected"].scope.account_id})).all()
    assert rows == [("fUST", 0, 0, 17, 0)]


async def test_prepared_artifact_records_managed_symbol_scope(archive_db, tmp_path):
    factory, kwargs = await prepare_fixture(archive_db)
    kwargs = {
        **kwargs,
        "managed_symbols": frozenset({"fUST"}),
        "snapshot": replace(kwargs["snapshot"], wallet_available={"fUST": Decimal("17")}),
    }
    prepared = await cli.prepare_archive(
        factory,
        **_prepare_kwargs(kwargs),
        output=tmp_path / "prepared-fust",
        dry_run=False,
    )

    assert prepared["managed_symbols"] == ["fUST"]


async def test_fust_only_cutover_rejects_existing_fusd_projection(archive_db, tmp_path):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path, history=True)
    args["snapshot"] = replace(
        args["snapshot"],
        wallet_available={"fUST": Decimal("17")},
    )
    args["managed_symbols"] = frozenset({"fUST"})
    before = await complete_raw(factory)

    with pytest.raises(ValueError, match="apply_out_of_scope_projection"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


async def test_fust_only_cutover_rejects_preexisting_fusd_position_before_rebuild(archive_db, tmp_path):
    factory, engine = archive_db
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO position_state(account_id,exchange_account_id,deployment_environment,"
            "symbol,available_amount,last_updated_ms) VALUES (:account_text,:account_uuid,'ci','fUSD',1,999)"
        ), {"account_text": str(UUID(int=100)), "account_uuid": UUID(int=100)})

    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    args["snapshot"] = replace(args["snapshot"], wallet_available={"fUST": Decimal("17")})
    args["managed_symbols"] = frozenset({"fUST"})
    before = await complete_raw(factory)

    with pytest.raises(ValueError, match="apply_out_of_scope_projection"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)

    assert await complete_raw(factory) == before


async def test_fust_only_cutover_allows_zero_fusd_scaffold(archive_db, tmp_path):
    factory, engine = archive_db
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO position_state(account_id,exchange_account_id,deployment_environment,"
            "symbol,last_updated_ms) VALUES (:account_text,:account_uuid,'ci','fUSD',999)"
        ), {"account_text": str(UUID(int=100)), "account_uuid": UUID(int=100)})

    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    args["snapshot"] = replace(args["snapshot"], wallet_available={"fUST": Decimal("17")})
    args["managed_symbols"] = frozenset({"fUST"})

    async with factory.begin() as session:
        await module.apply_cutover(session, **args)

    async with factory() as session:
        assert (await session.execute(text(
            "SELECT symbol, available_amount FROM position_state "
            "WHERE exchange_account_id=:account AND deployment_environment='ci'"
        ), {"account": args["expected"].scope.account_id})).all() == [("fUST", 17)]


@pytest.mark.parametrize("age_ms", [-1, 900_001])
async def test_new_apply_rejects_future_or_stale_restore_before_append(archive_db, tmp_path, monkeypatch, age_ms):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    args["now_ms"] = 1_000_000
    args["snapshot"] = replace(args["snapshot"], query_started_at_ms=999_800,
                               query_finished_at_ms=999_900, occurred_at_ms=999_900)
    args["archive_restore_receipt"]["observed_at_ms"] = 1_000_000 - age_ms
    monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)
    before = await complete_raw(factory)

    async def forbidden(*a, **kw):
        raise AssertionError("invalid restore freshness reached snapshot mutation")

    monkeypatch.setattr(module.PostgresEventStore, "append_snapshot", forbidden)
    with pytest.raises(ValueError, match="restore_receipt_time_invalid"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


@pytest.mark.parametrize("age_ms", [0, 900_000])
async def test_restore_window_boundaries_and_completed_repeat(archive_db, tmp_path, monkeypatch, age_ms):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    args["now_ms"] = 1_000_000
    args["snapshot"] = replace(args["snapshot"], query_started_at_ms=999_800,
                               query_finished_at_ms=999_900, occurred_at_ms=999_900)
    args["archive_restore_receipt"]["observed_at_ms"] = 1_000_000 - age_ms
    monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)
    async with factory.begin() as session:
        receipt = await module.apply_cutover(session, **args)
    committed = await complete_raw(factory)
    # Exact committed requests still validate current state with an old receipt
    # or a wall-clock rollback. Neither retry may append another event.
    for now_ms in (2_000_001, 0):
        async with factory.begin() as session:
            assert await module.apply_cutover(session, **{**args, "now_ms": now_ms}) == receipt
        assert await complete_raw(factory) == committed


@pytest.mark.parametrize("stage", ["lock", "source", "archive"])
async def test_restore_freshness_counts_elapsed_apply_time(archive_db, tmp_path, monkeypatch, stage):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    args["now_ms"] = 1_000_000
    args["snapshot"] = replace(args["snapshot"], query_started_at_ms=999_800,
                               query_finished_at_ms=999_900, occurred_at_ms=999_900)
    args["archive_restore_receipt"]["observed_at_ms"] = 100_000
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    owner, name = {
        "lock": (module.AccountEventWriter, "acquire_lock"),
        "source": (module, "_verify_live_archive"),
        "archive": (module, "capture_archive"),
    }[stage]
    original = getattr(owner, name)

    async def delayed(*a, **kw):
        result = await original(*a, **kw)
        clock[0] = 0.001
        return result

    monkeypatch.setattr(owner, name, delayed)
    if stage != "archive":
        async def forbidden(*a, **kw):
            raise AssertionError("elapsed restore deadline reached snapshot mutation")
        monkeypatch.setattr(module.PostgresEventStore, "append_snapshot", forbidden)
    before = await complete_raw(factory)
    with pytest.raises(ValueError, match="restore_receipt_time_invalid"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


@pytest.mark.parametrize("point", ["before_append", "after_append", "before_rebuild", "after_rebuild",
                                  "before_parity", "after_parity", "before_receipt", "after_receipt"])
async def test_boundary_failure_rolls_back_all_raw_tables(archive_db, tmp_path, monkeypatch, point):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    before = await complete_raw(factory)
    when, stage = point.split("_")
    owner, method = {
        "append": (module.PostgresEventStore, "append_snapshot"),
        "rebuild": (module.PostgresEventStore, "rebuild_snapshot_from_log"),
        "parity": (module, "_verify_parity"), "receipt": (module, "_insert_receipt"),
    }[stage]
    original = getattr(owner, method)

    async def fail(*a, **kw):
        if when == "after":
            await original(*a, **kw)
        raise RuntimeError("synthetic " + point)

    monkeypatch.setattr(owner, method, fail)
    with pytest.raises(RuntimeError, match="synthetic " + point):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


@pytest.mark.parametrize("mutation", [
    "receipt_target", "receipt_digest", "receipt_v1", "receipt_image", "receipt_network",
    "evidence_digest", "evidence_run", "evidence_scope", "evidence_image", "evidence_projector",
    "stale", "wallet", "unknown", "source_drift", "head_drift", "stream_drift", "halt", "roles", "role_grant", "operation", "quiescence",
])
async def test_preconditions_fail_without_mutation(archive_db, tmp_path, mutation):
    module = runtime()
    factory, args, kwargs = await fixture(archive_db, tmp_path)
    if mutation.startswith("receipt_"):
        receipt = args["archive_restore_receipt"]
        if mutation == "receipt_digest":
            receipt["archive_verification"]["archives"][0]["manifest_digest"] = "0" * 64
        else:
            key, value = {"receipt_target": ("target_run_id", str(UUID(int=1))),
                "receipt_v1": ("schema_version", 1), "receipt_image": ("verifier_image_digest", "sha256:" + "0" * 64),
                "receipt_network": ("network_internal", False)}[mutation]
            receipt[key] = value
    elif mutation.startswith("evidence_"):
        key, value = {"evidence_digest": ("digest", "0" * 64), "evidence_run": ("run_id", UUID(int=1)),
            "evidence_scope": ("scope", replace(args["expected"].scope, environment="prod")),
            "evidence_image": ("image_digest", "sha256:" + "0" * 64),
            "evidence_projector": ("projector_version", "other")}[mutation]
        args["evidence"] = replace(args["evidence"], diagnostic=replace(args["evidence"].diagnostic, **{key: value}))
    elif mutation == "stale":
        args["now_ms"] = 999999999
    elif mutation == "wallet":
        args["snapshot"] = replace(args["snapshot"], wallet_available={"fUST": Decimal("0")})
    elif mutation == "unknown":
        from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
        args["snapshot"] = replace(args["snapshot"], offers=(VenueOfferObservation(
            "1", "fUST", Decimal("2"), Decimal("1"), None, 2, "active", 900, 1000,
        ),))
    elif mutation in {"source_drift", "halt", "head_drift"}:
        async with factory.begin() as session:
            await session.execute(text({
                "source_drift": "UPDATE position_state SET reserved=99 WHERE deployment_environment='ci'",
                "halt": "UPDATE trading_halt SET halted=false",
                "head_drift": "INSERT INTO projection_heads(exchange_account_id,deployment_environment,projection_name,last_event_seq,projector_version) VALUES ('00000000-0000-0000-0000-000000000064','ci','execution_state',99,'execution-state-v1')",
            }[mutation]))
    elif mutation == "roles":
        args["runtime_roles"] = ("postgres",)
    elif mutation == "role_grant":
        with archive_db[1].begin() as connection:
            connection.exec_driver_sql(f"GRANT INSERT ON projection_audit.receipts TO {args['runtime_roles'][0]}")
    elif mutation == "stream_drift":
        async with factory.begin() as session:
            await module.PostgresEventStore(deployment_environment="ci").append_snapshot(session, kwargs["snapshot"])
    elif mutation == "operation":
        kwargs["runner"].running = True
    else:
        async def refused(*a, **kw):
            raise ValueError("quiescence refused")
        args["quiescence_verifier"] = refused
    before = await complete_raw(factory)
    with pytest.raises(ValueError):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


@pytest.mark.parametrize("change", ["snapshot", "operation", "receipt", "projection"])
async def test_completed_request_checks_identity_and_current_consistency(archive_db, tmp_path, change):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    async with factory.begin() as session:
        await module.apply_cutover(session, **args)
    if change == "snapshot":
        args["snapshot"] = replace(args["snapshot"], event_id=UUID(int=778))
    elif change == "operation":
        args["operation_digest"] = "0" * 64
    elif change == "receipt":
        args["archive_restore_receipt"]["restore_run_id"] = "another-restore"
    else:
        async with factory.begin() as session:
            await session.execute(text("UPDATE position_state SET available_amount=999 WHERE deployment_environment='ci'"))
    before = await complete_raw(factory)
    with pytest.raises(ValueError):
        async with factory.begin() as session:
            await module.apply_cutover(session, **{**args, "now_ms": 999999999})
    assert await complete_raw(factory) == before


async def test_concurrent_account_writer_fences_apply_before_checks(archive_db, tmp_path):
    from sqlalchemy.exc import DBAPIError
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    before = await complete_raw(factory)
    async with factory.begin() as writer:
        await module.AccountEventWriter(store=module.PostgresEventStore(deployment_environment="ci")).acquire_lock(
            writer, account_id=args["expected"].scope.account_id,
        )
        with pytest.raises(DBAPIError, match="lock timeout"):
            async with factory.begin() as session:
                await session.execute(text("SET LOCAL lock_timeout='50ms'"))
                await module.apply_cutover(session, **{**args, "now_ms": 999999999})
    assert await complete_raw(factory) == before


@pytest.mark.parametrize("column", ["available_amount", "offered_amount", "lent_amount", "uncertain_amount"])
async def test_database_parity_fault_cannot_be_hidden_by_identity_map(archive_db, tmp_path, monkeypatch, column):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    before = await complete_raw(factory)
    original = module._verify_parity

    async def corrupt(session, **kw):
        await session.execute(text(f"UPDATE public.position_state SET {column}=999 WHERE deployment_environment='ci'"))
        return await original(session, **kw)

    monkeypatch.setattr(module, "_verify_parity", corrupt)
    with pytest.raises(ValueError, match="parity"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


@pytest.mark.parametrize("stage", ["quiescence", "append", "archive"])
async def test_freshness_includes_time_spent_inside_apply(archive_db, tmp_path, monkeypatch, stage):
    import time
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    before = await complete_raw(factory)
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    if stage == "quiescence":
        original = args["quiescence_verifier"]
        async def delayed(*a, **kw):
            await original(*a, **kw)
            clock[0] = 301.0
        args["quiescence_verifier"] = delayed
    else:
        owner, name = (module.PostgresEventStore, "append_snapshot") if stage == "append" else (module, "capture_archive")
        original = getattr(owner, name)
        async def delayed(*a, **kw):
            result = await original(*a, **kw)
            clock[0] = 301.0
            return result
        monkeypatch.setattr(owner, name, delayed)
    with pytest.raises(ValueError, match="snapshot_time"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


async def test_drift_after_parity_before_archive_is_rejected(archive_db, tmp_path, monkeypatch):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    before = await complete_raw(factory)
    original = module.capture_archive
    async def corrupt(session, **kw):
        await session.execute(text("UPDATE public.position_state SET available_amount=999 WHERE deployment_environment='ci'"))
        return await original(session, **kw)
    monkeypatch.setattr(module, "capture_archive", corrupt)
    with pytest.raises(ValueError, match="parity"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


async def test_nonempty_offer_credit_wallet_dimensions_and_caller_rollback(archive_db, tmp_path):
    from bfx_funding_bot.modules.execution.event_store.entities import (
        VenueCreditObservation,
        VenueOfferObservation,
    )
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    args["snapshot"] = replace(args["snapshot"],
        offers=(VenueOfferObservation("o1", "fUST", Decimal("9"), Decimal("7"), Decimal("0.001"), 2, "active", 900, 1000),),
        credits=(VenueCreditObservation("c1", "fUSD", Decimal("11"), Decimal("0.002"), 3, "active", 900, 1000),))
    before = await complete_raw(factory)
    async with factory() as session, session.begin():
        await module.apply_cutover(session, **args)
        assert (await session.execute(text(
            "SELECT symbol, offered_amount, lent_amount, available_amount, uncertain_amount "
            "FROM position_state WHERE deployment_environment='ci' ORDER BY symbol"
        ))).all() == [("fUSD", 0, 11, 2, 0), ("fUST", 7, 0, 0, 0)]
        await session.rollback()
    assert await complete_raw(factory) == before


async def test_completed_retry_reads_fresh_events_despite_loaded_identity_map(archive_db, tmp_path):
    from sqlalchemy import select

    from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    async with factory.begin() as session:
        await module.apply_cutover(session, **args)
    before = await complete_raw(factory)
    async with factory() as session:
        loaded = (await session.scalars(select(EventLogRow))).one()
        await session.execute(text("UPDATE public.event_log SET occurred_at_ms=999 WHERE deployment_environment='ci'"))
        assert loaded.occurred_at_ms == 1400
        with pytest.raises(ValueError, match="stream"):
            await module.apply_cutover(session, **{**args, "now_ms": 999999999})
        await session.rollback()
    assert await complete_raw(factory) == before


async def test_actual_appended_event_must_equal_requested_snapshot(archive_db, tmp_path, monkeypatch):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path)
    before = await complete_raw(factory)
    original = module.PostgresEventStore.append_snapshot
    async def corrupt(self, session, event):
        result = await original(self, session, event)
        await session.execute(text(
            "UPDATE public.event_log SET payload=jsonb_set(payload,'{query_started_at_ms}','1299') "
            "WHERE event_id=:id"
        ), {"id": event.event_id})
        return result
    monkeypatch.setattr(module.PostgresEventStore, "append_snapshot", corrupt)
    with pytest.raises(ValueError, match=r"snapshot|event"):
        async with factory.begin() as session:
            await module.apply_cutover(session, **args)
    assert await complete_raw(factory) == before


@pytest.mark.parametrize("corrupt_prefix", [False, True])
async def test_existing_prefix_and_noncontiguous_actual_head(archive_db, tmp_path, monkeypatch, corrupt_prefix):
    module = runtime()
    factory, args, _ = await fixture(archive_db, tmp_path, history=True)
    before = await complete_raw(factory)
    original = module.PostgresEventStore.append_snapshot
    async def append(self, session, snapshot):
        await session.execute(text("SELECT nextval('public.event_log_event_seq_seq')"))
        result = await original(self, session, snapshot)
        if corrupt_prefix:
            await session.execute(text("UPDATE public.event_log SET occurred_at_ms=999 WHERE event_seq=:head"),
                                  {"head": args["expected"].stream.head})
        return result
    monkeypatch.setattr(module.PostgresEventStore, "append_snapshot", append)
    if corrupt_prefix:
        with pytest.raises(ValueError, match="prefix"):
            async with factory.begin() as session:
                await module.apply_cutover(session, **args)
        assert await complete_raw(factory) == before
    else:
        async with factory.begin() as session:
            result = await module.apply_cutover(session, **args)
        after = await complete_raw(factory)
        assert set(before["public.event_log"]) < set(after["public.event_log"])
        assert result["new_stream"]["count"] == 2
        assert result["new_stream"]["head"] == args["expected"].stream.head + 2


async def _release_history(factory, *, stale_head):
    """Hand-checked legacy input: two completed CID cycles and audit-only credits."""
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.event_store.tables import (
        EventLogRow,
        OfferClaimRow,
        PositionStateRow,
        ProjectionHeadRow,
    )
    from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows

    account, other = UUID(int=100), UUID(int=101)
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=other, venue="bitfinex", label="synthetic-other"))
        rows = historical_rows(first_amount="7", environment="ci")
        for row in rows:
            row.account_id = row.payload["account_id"] = str(account)
            row.exchange_account_id = account
        session.add_all(rows)
        for symbol in ("fUST", "fUSD"):
            session.add(EventLogRow(
                account_id=str(account), exchange_account_id=account,
                deployment_environment="ci", event_type="CREDIT_CLOSED", schema_version=2,
                payload={"account_id": str(account), "symbol": symbol, "amount": "999",
                         "is_simulated": True}, occurred_at_ms=7000,
            ))
        await session.flush()
        await PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
            session, account_id=str(account), deployment_environment="ci",
        )
        # Genesis replay must count both fills (7+5), never credit close amounts
        # or the old source-only checkpoint seeded by archive_db.
        position = (await session.scalars(select(PositionStateRow).where(
            PositionStateRow.exchange_account_id == account,
            PositionStateRow.deployment_environment == "ci",
        ))).one()
        assert (position.symbol, position.reserved, position.realized) == ("fUST", 0, 12)
        claim = (await session.scalars(select(OfferClaimRow))).one()
        assert (claim.cid, claim.state, claim.venue_offer_id, claim.size_usdt) == (
            81, "released", "old-b", 5,
        )
        claim.last_updated_ms = 999
        head = (await session.scalars(select(ProjectionHeadRow))).one()
        if stale_head:
            head.last_event_seq = 0
        head.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
        # Keep an old checkpoint that contradicts genesis; archive preserves it,
        # while a new complete snapshot replaces its runtime authority.
        await session.execute(text(
            "INSERT INTO reconcile_observation(account_id,exchange_account_id,deployment_environment,"
            "reserved_usdt,realized_usdt,n_offers,n_credits,observed_at_ms,event_seq_fence,recorded_at) "
            "VALUES (:s,:id,'ci',0,999,0,1,7000,6,'2000-01-01T00:00:00Z')"
        ), {"s": str(account), "id": account})
        for owner, environment, symbol in (
            (account, "ci", "fEUR"), (other, "ci", "fGBP"), (account, "shadow", "fJPY"),
        ):
            session.add(PositionStateRow(
                account_id=str(owner), exchange_account_id=owner,
                deployment_environment=environment, symbol=symbol,
                reserved=Decimal("33"), realized=Decimal("44"), last_updated_ms=55,
                last_event_seq=0,
            ))
        await session.execute(text(
            "INSERT INTO trading_halt(account_id,exchange_account_id,deployment_environment,"
            "halted,reason,actor,created_at_ms) VALUES (:s,:a,'ci',true,'synthetic','test',8000)"
        ), {"s": str(account), "a": account})


async def _release_evidence(factory, tmp_path):
    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope
    from bfx_funding_bot.modules.execution.projection_cutover.evidence import (
        CLASSIFICATION_KIND,
        DIAGNOSTIC_KIND,
        EvidenceWriter,
        iter_verified_records,
    )

    identity = {"scope": Scope(UUID(int=100), "ci"), "run_id": uuid4(),
                "image_digest": "sha256:" + "a" * 64, "projector_version": "execution-state-v1"}
    diag = await cli.diagnose(factory, **identity, output=tmp_path / "diagnostic")
    writer = EvidenceWriter.create(tmp_path / "classification", manifest_fields={
        **identity, "kind": CLASSIFICATION_KIND, "format_version": 2,
        "stream": diag.stream, "record_kind": "classification",
        "diagnostic_digest": diag.digest, "reviewer": "synthetic-test-review",
    })
    differences = list(iter_verified_records(
        tmp_path / "diagnostic", expected_digest=diag.digest, expected_kind=DIAGNOSTIC_KIND,
    ))
    # Exact categories are fixture-specific, not a blanket production approval.
    reasons = {
        "offer_claims": ("time_sequence", "claim timestamp deliberately set to 999"),
        "projection_heads": ("time_sequence", "head timestamp set to year 2000; optional stale cursor"),
        "position_state": ("missing_symbol", "source-only fEUR is absent from genesis events"),
        "reconcile_observation": ("checkpoint_source", "source-only checkpoint is not an event"),
    }
    assert {d["table"] for d in differences} == set(reasons)
    for difference in differences:
        category, reason = reasons[difference["table"]]
        writer.append({**difference, "classification": category, "reason": reason,
                       "evidence": "synthetic _release_history fixture"})
    classified = writer.finish()
    verified = cli.verify_cutover_evidence(
        tmp_path / "diagnostic", tmp_path / "classification",
        expected_diagnostic_digest=diag.digest, expected_classification_digest=classified.digest,
        **{"expected_" + k: v for k, v in identity.items()},
    )
    assert sum(dict(verified.classification_counts).values()) == len(differences) > 0
    return identity, verified


@asynccontextmanager
async def _restored_release_copy(archive_db):
    """Real PG18 dump/restore into a different DB on this testcontainer only.

    No R2/PITR/network-isolation/RTO claim: those require the operator drill.
    Container identity comes exclusively from archive_db's disposable fixture.
    """
    from docker.errors import NotFound
    from testcontainers.core.docker_client import DockerClient

    url = archive_db[1].url
    assert url.host in {"localhost", "127.0.0.1"} and url.database == "test"
    client = DockerClient().client
    candidates = [c for c in client.containers.list() if any(
        b["HostPort"] == str(url.port)
        for b in c.attrs["NetworkSettings"]["Ports"].get("5432/tcp", []) or []
    )]
    assert len(candidates) == 1
    container = candidates[0]
    assert container.attrs["Config"]["Image"] == "postgres:18-alpine"
    name = "task6_restore_" + uuid4().hex
    dump_path = "/tmp/" + name + ".dump"
    restored_engine = create_async_engine(url.set(
        drivername="postgresql+asyncpg", database=name,
    ))

    def execute(argv):
        result = container.exec_run(argv)
        assert result.exit_code == 0, result.output.decode()

    try:
        execute(["pg_dump", "--username=test", "--dbname=test", "--format=custom",
                 "--no-owner", "--no-privileges", "--file=" + dump_path])
        execute(["createdb", "--username=test", name])
        execute(["pg_restore", "--username=test", "--dbname=" + name,
                 "--no-owner", "--no-privileges", "--exit-on-error", dump_path])
        yield async_sessionmaker(restored_engine, expire_on_commit=False)
    finally:
        await restored_engine.dispose()
        # Generated DB/file only; the source fixture and its container survive.
        execute(["dropdb", "--username=test", "--if-exists", name])
        execute(["rm", "-f", "--", dump_path])
        with archive_db[1].connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM pg_database WHERE datname=:n"),
                                     {"n": name}) == 0
        assert container.exec_run(["test", "!", "-e", dump_path]).exit_code == 0
        # Container still exists; cleanup must not delete its owning fixture.
        try:
            client.containers.get(container.id)
        except NotFound:
            pytest.fail("rehearsal cleanup removed the source testcontainer")
        client.close()


@pytest.mark.parametrize("stale_head", [False, True])
async def test_release_handoff_restores_history_then_verifies_new_baseline(archive_db, tmp_path, stale_head):
    """Catch lost history, partial zero coverage, scope leaks and self-derived DR expectations."""
    from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
    from bfx_funding_bot.modules.execution.events import SnapshotCoverage
    from scripts.verify_projection_replay import replay_one_account
    from tests.modules.execution.projection_cutover.test_snapshot import snapshot
    from tests.scripts.test_cutover_projection import FixedOperations, operation_inventory
    from tests.scripts.test_offsite_dr_restore import evidence as dr_evidence

    factory, engine = archive_db
    await _release_history(factory, stale_head=stale_head)
    before = await complete_raw(factory)
    assert len(before["public.event_log"]) == 8
    identity, verified = await _release_evidence(factory, tmp_path)
    assert await complete_raw(factory) == before
    role = "task6_runtime_" + uuid4().hex
    with engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} LOGIN")
    inventory = {**operation_inventory(), "runtime_roles": dict.fromkeys(
        ("bot", "webapi", "frontend", "weekly-report"), role,
    )}
    runner = FixedOperations()  # Only OS probes are substituted; DB checks are real.
    initial_snapshot = snapshot(
        event_id=uuid4(), query_started_at_ms=9000, query_finished_at_ms=9100,
        occurred_at_ms=9100, wallet_available={"fUST": Decimal("17"), "fUSD": Decimal("0")},
    )
    prepared = await cli.prepare_archive(
        factory, **identity, evidence=verified, snapshot=initial_snapshot,
        managed_symbols=frozenset({"fUST", "fUSD"}), now_ms=9200, max_age_ms=300_000,
        operation_inventory=inventory, runtime_roles=(role,), output=tmp_path / "prepared",
        dry_run=False, runner=runner,
    )
    expected = decode_manifest(encode_row(prepared["manifest"]))
    assert expected.stream.count == 8
    archived = await complete_raw(factory)
    for name in before:
        if name.startswith("public."):
            assert archived[name] == before[name]

    def reference(payload, manifest, path):
        cli.write_private(path, payload)
        return {"run_id": str(manifest.run_id), "manifest_digest": manifest.digest,
                "prepared_path": str(path), "prepared_digest": hashlib.sha256(payload).hexdigest()}

    original_ref = reference(encode_row(prepared), expected, tmp_path / "independent-prepared")
    raw = dr_evidence._archive.transport((original_ref,), target_run_id=str(expected.run_id))
    async with _restored_release_copy(archive_db) as restored:
        assert await complete_raw(restored) == archived
        async with restored() as session:
            report = await verify_restore_archives(session, scope=expected.scope, raw=raw, archive_only=True)
            assert await session.scalar(text("SELECT current_database()")) != "test"
            old_replay = await replay_one_account(session, account_id=expected.scope.account_id,
                environment="ci", projector_version=expected.projector_version)
            assert any(not d["matches"] for d in old_replay.diagnostic_diff.values())
        assert report["event_hash"] == expected.stream.digest
        assert report["archives"][0]["verified_counts"]["reconcile_observation"] == 1
        assert await complete_raw(restored) == archived

    # Test-only receipt envelope. Its archive report above is actually measured
    # on a restored DB; synthetic image/network/RTO fields are NOT release evidence.
    restore_receipt = {
        "schema_version": 2, "kind": "archive_restore", "measured": True,
        "target_run_id": str(expected.run_id), "archive_verification": report,
        "restore_run_id": "synthetic-task6", "archive_input_digest": hashlib.sha256(raw).hexdigest(),
        "target_backup_label": "synthetic-task6", "target_time": None,
        "config_digest": "b" * 64, "observed_at_ms": 10000, "rto_seconds": 0,
        "verifier_image_digest": expected.image_digest, "image_digest": "sha256:" + "c" * 64,
        "image_labels": {}, "server_version_num": 180000,
        "migration_heads": list(expected.migration_heads), "event_count": 8,
        "event_head": expected.stream.head, "event_hash": expected.stream.digest,
        "account_id": str(expected.scope.account_id), "environment": "ci",
        "projector_version": expected.projector_version, "network_internal": True,
        "egress_disconnected": True, "verifier_exit_status": 0, "network_name": "bfx-dr-synthetic",
    }
    fresh = replace(initial_snapshot, event_id=uuid4(), query_started_at_ms=11000,
                    query_finished_at_ms=11100, occurred_at_ms=11100)
    assert fresh.coverage == SnapshotCoverage(True, True, True)
    assert fresh.wallet_available == {"fUST": 17, "fUSD": 0}
    assert fresh.offers == fresh.credits == ()

    async def quiescence(session, *, scope, runtime_roles, operation_digest):
        assert operation_digest == row_digest(inventory)
        await verify_database_quiescence(session, scope=scope, runtime_roles=runtime_roles)
        await verify_local_operations(inventory, runner=runner)

    args = {"expected": expected, "snapshot": fresh, "archive_restore_receipt": restore_receipt,
            "evidence": verified, "runtime_roles": (role,), "operation_digest": row_digest(inventory),
            "quiescence_verifier": quiescence, "now_ms": 11200}
    # Missing fUSD is not zero coverage; neither is an unexpected venue symbol.
    for invalid in (
        replace(fresh, wallet_available={"fUST": Decimal("17")}),
        replace(fresh, coverage=SnapshotCoverage(True, False, True)),
        replace(fresh, wallet_available={**fresh.wallet_available, "fEUR": Decimal("0")}),
    ):
        with pytest.raises(ValueError):
            async with factory.begin() as session:
                await runtime().apply_cutover(session, **{**args, "snapshot": invalid})
        assert await complete_raw(factory) == archived
    if stale_head:
        from bfx_funding_bot.modules.execution.event_store.writer import ProjectionWriteError

        with pytest.raises(ProjectionWriteError, match="claim identity conflict"):
            async with factory.begin() as session:
                await runtime().apply_cutover(session, **args)
        assert await complete_raw(factory) == archived
        return  # Known release gate; do not bypass strict append semantics.
    async with factory.begin() as session:
        applied = await runtime().apply_cutover(session, **args)
    committed = await complete_raw(factory)
    assert set(before["public.event_log"]) < set(committed["public.event_log"])
    assert len(committed["public.event_log"]) == 9
    assert set(archived["projection_audit.rows"]) < set(committed["projection_audit.rows"])
    assert len(committed["projection_audit.runs"]) == 2
    assert len(committed["projection_audit.receipts"]) == 1
    assert committed["public.trading_halt"] == before["public.trading_halt"]
    for name in ("position_state", "offer_claims", "reconcile_observation", "projection_heads"):
        def outside_scope(rows):
            return [r for r in rows if (json.loads(r)["exchange_account_id"],
                    json.loads(r)["deployment_environment"]) != (str(expected.scope.account_id), "ci")]
        assert outside_scope(committed["public." + name]) == outside_scope(before["public." + name])
    async with factory() as session:
        assert (await session.execute(text(
            "SELECT symbol,offered_amount,lent_amount,available_amount,uncertain_amount "
            "FROM position_state WHERE exchange_account_id=:id AND deployment_environment='ci' ORDER BY symbol"
        ), {"id": expected.scope.account_id})).all() == [("fUSD", 0, 0, 0, 0), ("fUST", 0, 0, 17, 0)]
        archived_checkpoints = await session.scalars(text(
            "SELECT encoded_payload FROM projection_audit.rows WHERE run_id=:id AND table_name='reconcile_observation'"
        ), {"id": expected.run_id})
        assert [decode_row(p)["realized_usdt"] for p in archived_checkpoints] == [999]
    async with factory.begin() as session:
        assert await runtime().apply_cutover(session, **{**args, "now_ms": 999999999}) == applied
    assert await complete_raw(factory) == committed

    # Capture expected new baseline on SOURCE before restoring. Do not derive it
    # from apply's receipt or the restored DB being accepted.
    async with factory() as session:
        baseline_replay = await replay_one_account(session, account_id=expected.scope.account_id,
            environment="ci", projector_version=expected.projector_version)
        heads = tuple(await session.scalars(text("SELECT version_num FROM alembic_version ORDER BY version_num")))
        credit_rows = (await session.scalars(select(EventLogRow).where(EventLogRow.event_type == "CREDIT_CLOSED"))).all()
        assert len(credit_rows) == 2 and all(r.schema_version == 2 for r in credit_rows)
    assert baseline_replay.row_counts["event_log"] == 9
    assert all(d["matches"] for d in baseline_replay.diagnostic_diff.values())
    active = decode_manifest(encode_row(applied["applied_manifest"]))
    active_ref = reference(encode_row({**prepared, "manifest": applied["applied_manifest"]}),
                           active, tmp_path / "independent-applied")
    baseline = dr_evidence.RestoreBaseline(
        target_backup_label="synthetic-task6-post-apply", target_time=None, database_name="test",
        account_id=str(expected.scope.account_id), environment="ci", projector_version=expected.projector_version,
        migration_heads=heads, event_count=9, event_head=baseline_replay.event_head,
        event_hash=baseline_replay.event_hash, schema_version=2, archives=(original_ref, active_ref),
        verifier_image_digest=expected.image_digest,
    )
    complete_input = dr_evidence._archive.transport(baseline.archives)
    async with _restored_release_copy(archive_db) as restored:
        assert await complete_raw(restored) == committed
        async with restored() as session:
            archive_report = await verify_restore_archives(session, scope=expected.scope,
                raw=complete_input, archive_only=False)
            dr_evidence._archive.validate_report(json.dumps(archive_report), baseline=baseline, archive_only=False)
            replay = await replay_one_account(session, account_id=expected.scope.account_id,
                environment="ci", projector_version=expected.projector_version,
                expected_event_hash=baseline.event_hash)
        assert replay.event_head == baseline.event_head
        assert replay.row_counts == baseline_replay.row_counts
        assert replay.content_hashes == baseline_replay.content_hashes
        assert all(d["matches"] for d in replay.diagnostic_diff.values())
        assert {r["run_id"] for r in archive_report["archives"]} == {str(expected.run_id), str(active.run_id)}
        assert await complete_raw(restored) == committed
    assert await complete_raw(factory) == committed
