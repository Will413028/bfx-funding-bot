"""Task 5: real PostgreSQL atomicity with verified v2 directory evidence."""

import base64
import hashlib
import importlib
import json
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.execution.projection_cutover.codec import encode_row, row_digest
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
