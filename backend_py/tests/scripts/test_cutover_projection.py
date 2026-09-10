"""Prepare evidence must share the archive's lossless PostgreSQL representation."""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.execution.projection_cutover.codec import JSON_NULL, encode_row
from scripts.verify_projection_replay import _archive_projection_rows
from tests.integration.test_projection_cutover_archive import archive_db as _archive_db
from tests.integration.test_projection_cutover_archive import archive_pg as _archive_pg

archive_db = _archive_db
archive_pg = _archive_pg


def cli():
    from scripts import cutover_projection

    return cutover_projection


@pytest.mark.integration
@pytest.mark.parametrize(
    ("json_text", "expected"),
    [
        (None, None),
        ("null", JSON_NULL),
        ('{"amount":1.234567890123456789,"nested":null}',
         {"amount": Decimal("1.234567890123456789"), "nested": None}),
    ],
    ids=["sql-null", "json-null", "numeric-json"],
)
async def test_diagnostic_source_matches_archive_json_contract(
    pg_session_factory, json_text, expected,
):
    """Catch null conflation/float rounding before classification binds to archive.

    A temporary physical copy avoids modifying the runtime uncertainty constraints;
    the production reader must preserve the exact physical JSON column value.
    """
    account = UUID(int=100)
    async with pg_session_factory() as session:
        await session.execute(text(
            "CREATE TEMP TABLE execution_uncertainties "
            "(LIKE public.execution_uncertainties) ON COMMIT DROP"
        ))
        await session.execute(text(
            "INSERT INTO execution_uncertainties "
            "(uncertainty_id,exchange_account_id,deployment_environment,symbol,kind,"
            "correlation_key,state,intended_amount,evidence,opened_event_seq,opened_at,"
            "resolution_evidence) VALUES "
            "(:id,:id,'ci','fUST','unsupported_venue_exposure','synthetic','open',0,"
            "'{}'::jsonb,1,'2000-01-01T00:00:00Z',CAST(:payload AS jsonb))"
        ), {"id": account, "payload": json_text})
        rows = await _archive_projection_rows(
            session, account_id=account, environment="ci",
        )
        actual = rows["execution_uncertainties"][0]["resolution_evidence"]
        assert actual == expected
        # Equality alone can conceal some float/Decimal representation changes.
        assert encode_row({"value": actual}) == encode_row({"value": expected})


@pytest.mark.parametrize("args", [[], ["prepare"], ["apply"], ["diagnose", "prepare"],
                                   ["prepare", "--account-id", "SECRET-INVALID"]])
def test_cli_missing_identity_and_apply_never_mutate_or_echo(args, capsys):
    assert cli().main(args) != 0
    output = capsys.readouterr()
    assert "SECRET-INVALID" not in output.out + output.err


def test_private_evidence_exclusive_permissions_and_symlinks(tmp_path):
    path = tmp_path / "evidence"
    cli().write_private(path, b"synthetic")
    assert path.read_bytes() == b"synthetic"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises((ValueError, FileExistsError)):
        cli().write_private(path, b"changed")
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises((ValueError, FileExistsError)):
        cli().write_private(link, b"changed")
    assert path.read_bytes() == b"synthetic"


def test_private_read_pins_digest_and_refuses_symlink_or_public_mode(tmp_path):
    import hashlib

    path = tmp_path / "evidence"
    cli().write_private(path, b"synthetic")
    digest = hashlib.sha256(b"synthetic").hexdigest()
    assert cli().read_private(path, expected_digest=digest) == b"synthetic"
    with pytest.raises(ValueError):
        cli().read_private(path, expected_digest="0" * 64)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises((ValueError, OSError)):
        cli().read_private(link, expected_digest=digest)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        cli().read_private(path, expected_digest=digest)


@pytest.mark.parametrize("wallets,success", [
    ([["funding", "UST", "1", "0", "1"], ["funding", "USD", "0", "0", "0"]], True),
    ([["funding", "UST", "1", "0", "1"]], False),
    ([["funding", "UST", "1", "0", None], ["funding", "USD", "0", "0", "0"]], False),
])
async def test_real_parser_requires_explicit_wallet_evidence(wallets, success):
    import httpx

    from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
    from tests.modules.execution.projection_cutover.test_snapshot import SCOPE, SYMBOLS

    seen = []

    def respond(request):
        seen.append(request.url.path)
        assert request.method == "POST"
        assert request.content == b"{}"
        assert request.url.path in {
            "/v2/auth/r/funding/offers", "/v2/auth/r/funding/credits", "/v2/auth/r/wallets",
        }
        return httpx.Response(200, json=wallets if request.url.path.endswith("wallets") else [])

    kwargs = {
        "scope": SCOPE, "managed_symbols": SYMBOLS, "max_age_ms": 300_000,
        "ctx": AccountContext(account_id=str(SCOPE.account_id),
                           credentials=Credentials(api_key="synthetic", api_secret="synthetic"),
                           allocation_cap_usdt=Decimal("0")),
        "transport": httpx.MockTransport(respond),
    }
    if success:
        result = await cli().collect_snapshot(**kwargs)
        assert result.wallet_available == {"fUST": Decimal("1"), "fUSD": Decimal("0")}
        assert len(seen) == 3
    else:
        with pytest.raises(ValueError):
            await cli().collect_snapshot(**kwargs)


def diagnostic():
    return {
        "kind": "projection-cutover-diagnostic-v1", "run_id": UUID(int=99),
        "scope": {"account_id": UUID(int=100), "environment": "ci"},
        "image_digest": "sha256:" + "a" * 64, "projector_version": "execution-state-v1",
        "stream": {"count": 0, "head": 0, "digest": "b" * 64},
        "tables": [], "differences": [{
            "table": "position_state", "key_digest": "c" * 64, "column": "last_updated_ms",
            "before_digest": "d" * 64, "after_digest": "e" * 64,
            "classification": "unexplained",
        }],
    }


def classification(diag):
    from bfx_funding_bot.modules.execution.projection_cutover.codec import row_digest

    return {
        "diagnostic_digest": row_digest(diag), "reviewer": "synthetic-operator",
        "classifications": [{
            **diag["differences"][0], "classification": "time_sequence",
            "reason": "original timestamp differs from replay event timestamp",
            "evidence": "event-seq-1-versus-original-row",
        }],
    }


def test_review_binds_every_difference_and_artifact_digest():
    import hashlib

    diag = diagnostic()
    payload = encode_row(classification(diag))
    cli().validate_classification(diag, payload, expected_digest=hashlib.sha256(payload).hexdigest())
    with pytest.raises(ValueError):
        cli().validate_classification(diag, payload, expected_digest="0" * 64)


@pytest.mark.parametrize("mutation", ["empty", "duplicate", "new", "unexplained", "reason",
                                     "evidence", "reviewer", "diagnostic_digest"])
def test_review_rejects_unreviewed_missing_new_or_drifted_difference(mutation):
    import hashlib

    diag = diagnostic()
    review = classification(diag)
    if mutation == "empty":
        review["classifications"] = []
    elif mutation == "duplicate":
        review["classifications"] *= 2
    elif mutation == "new":
        review["classifications"][0]["before_digest"] = "0" * 64
    elif mutation == "unexplained":
        review["classifications"][0]["classification"] = "unexplained"
    elif mutation in {"reviewer", "diagnostic_digest"}:
        review[mutation] = ""
    else:
        review["classifications"][0][mutation] = ""
    payload = encode_row(review)
    with pytest.raises(ValueError):
        cli().validate_classification(diag, payload, expected_digest=hashlib.sha256(payload).hexdigest())


@pytest.mark.integration
async def test_diagnose_captures_all_original_columns_without_mutation(pg_session_factory):
    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope
    from tests.integration.test_projection_cutover_diagnostics import _seed, _source_state
    from tests.modules.execution.event_store.test_historical_claim_cycles import ACCOUNT

    await _seed(pg_session_factory)
    before = await _source_state(pg_session_factory)
    result = await cli().diagnose(
        pg_session_factory, scope=Scope(ACCOUNT, "ci"), run_id=UUID(int=99),
        image_digest="sha256:" + "a" * 64, projector_version="execution-state-v1",
    )
    assert result["stream"]["count"] > 0
    assert len(result["tables"]) == 8
    assert all(d["classification"] == "unexplained" for d in result["differences"])
    assert any(d["column"] == "last_updated_ms" for d in result["differences"])
    assert await _source_state(pg_session_factory) == before


@pytest.mark.integration
async def test_archive_and_diagnostic_full_columns_share_real_pg_bytes(archive_db):
    from bfx_funding_bot.modules.execution.projection_cutover.archive import capture_archive
    from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row
    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope

    factory, engine = archive_db
    account = UUID(int=100)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO event_log(account_id,exchange_account_id,deployment_environment,"
            "event_type,payload,occurred_at_ms) "
            "SELECT :s,:a,'ci','CREDIT_CLOSED','{}'::jsonb,i FROM generate_series(1,3) i"
        ), {"s": str(account), "a": account})
        connection.execute(text(
            "INSERT INTO execution_uncertainties(exchange_account_id,deployment_environment,"
            "symbol,kind,correlation_key,intended_amount,evidence,opened_event_seq) "
            "VALUES (:a,'ci','fUST','submit_outcome_unknown','sql-null',1,'{}',1)"
        ), {"a": account})
        for key, payload in [("json-null", "null"), ("numeric", '{"amount":1.234567890123456789,"nested":null}')]:
            connection.execute(text(
                "INSERT INTO execution_uncertainties(exchange_account_id,deployment_environment,"
                "symbol,kind,correlation_key,intended_amount,evidence,opened_event_seq,state,"
                "reconcile_event_seq,resolved_event_seq,resolved_by_operator_id,resolution_reason,"
                "resolution_evidence,resolved_at) VALUES "
                "(:a,'ci','fUST','submit_outcome_unknown',:key,1,'{}',1,'resolved',2,3,"
                "'synthetic','fixture',CAST(:payload AS jsonb),now())"
            ), {"a": account, "key": key, "payload": payload})
    async with factory.begin() as session:
        source = await _archive_projection_rows(session, account_id=account, environment="ci")
        manifest = await capture_archive(
            session, scope=Scope(account, "ci"), run_id=UUID(int=99),
            image_digest="sha256:" + "a" * 64, projector_version="execution-state-v1",
        )
        for table in manifest.tables:
            payloads = list((await session.execute(text(
                "SELECT encoded_payload FROM projection_audit.rows "
                "WHERE run_id=:id AND table_name=:name ORDER BY row_key"
            ), {"id": manifest.run_id, "name": table["name"]})).scalars())
            originals = sorted(source[table["name"]], key=lambda row: encode_row(
                {key: row[key] for key in table["key_columns"]}
            ))
            assert payloads == [encode_row(row) for row in originals]
            assert [decode_row(payload) for payload in payloads] == originals


def operations():
    from scripts import projection_cutover_operations

    return projection_cutover_operations


def operation_inventory():
    return {"project": "bfx", "daemon_id": "synthetic-daemon", "images": dict.fromkeys(("bot", "webapi", "frontend", "autoheal", "migrate", "weekly-report", "postgres", "redis"), "sha256:" + "a" * 64)}


class FixedOperations:
    """Substitute only local OS reads; inspect/validation runs in production code."""

    def __init__(self):
        self.running = False
        self.restart = "no"
        self.unit_state = "masked"
        self.unknown_unit = False
        self.remote = False
        self.oneoff = False
        self.calls = []

    async def __call__(self, argv):
        import json

        self.calls.append(argv)
        if argv[0] == "/usr/bin/docker":
            if "context" in argv:
                return json.dumps("tcp://remote:2375" if self.remote else "unix:///var/run/docker.sock").encode()
            assert argv[1:3] == ("--host", "unix:///var/run/docker.sock")
            if "info" in argv:
                return b'"synthetic-daemon"'
            if "ps" in argv:
                return "\n".join(f"{n:064x}" for n in range(1, 5)).encode()
            if "inspect" in argv:
                number = int(argv[-1], 16)
                return json.dumps({
                    "id": argv[-1], "project": "bfx", "service": ("bot", "webapi", "frontend", "autoheal")[number - 1], "oneoff": "True" if self.oneoff else "False",
                    "image": "sha256:" + "a" * 64, "running": self.running, "paused": False,
                    "restarting": False, "status": "exited", "restart": self.restart,
                }).encode()
        if "list-unit-files" in argv:
            return ("\n".join(f"{unit} masked -" for unit in (
                "bfx-weekly-report.service", "bfx-weekly-report.timer",
                "bfx-halt-watch.service", "bfx-halt-watch.timer",
            )) + ("\nbfx-surprise.timer enabled -" if self.unknown_unit else "")).encode()
        if "list-units" in argv:
            return b""
        if "show" in argv:
            return f"LoadState=masked\nActiveState=inactive\nSubState=dead\nUnitFileState={self.unit_state}\nJob=\n".encode()
        raise AssertionError(argv)


async def test_operational_inventory_uses_fixed_local_commands():
    runner = FixedOperations()
    await operations().verify_local_operations(operation_inventory(), runner=runner, environ={})
    assert any("list-units" in call for call in runner.calls)
    assert any("list-unit-files" in call for call in runner.calls)
    assert all(not any("Env" in argument for argument in call) for call in runner.calls)


@pytest.mark.parametrize("mutation", ["running", "restart", "unit_state", "unknown_unit", "remote"])
async def test_operational_inventory_refuses_writer_or_recreate_source(mutation):
    runner = FixedOperations()
    setattr(runner, mutation, {"restart": "unless-stopped", "unit_state": "enabled"}.get(mutation, True))
    with pytest.raises(ValueError):
        await operations().verify_local_operations(operation_inventory(), runner=runner, environ={})


@pytest.mark.parametrize("env", [{"DOCKER_HOST": "tcp://secret-host:2375"},
                                  {"DOCKER_CONTEXT": "remote"}])
async def test_remote_docker_environment_never_executes_a_probe(env):
    runner = FixedOperations()
    with pytest.raises(ValueError):
        await operations().verify_local_operations(operation_inventory(), runner=runner, environ=env)
    assert runner.calls == []


async def test_oneoff_cannot_replace_expected_regular_service_inventory():
    runner = FixedOperations()
    runner.oneoff = True
    with pytest.raises(ValueError):
        await operations().verify_local_operations(operation_inventory(), runner=runner, environ={})


async def test_unit_inventory_includes_socket_path_and_transient_units():
    runner = FixedOperations()

    async def probe(argv):
        if "list-units" in argv and not any(argument.startswith("--type") for argument in argv):
            return b"bfx-unexpected.socket loaded active listening synthetic"
        return await runner(argv)

    with pytest.raises(ValueError):
        await operations().verify_local_operations(operation_inventory(), runner=probe, environ={})


@pytest.mark.parametrize("payload", [b"", b"x" * 65537, b"not-json"])
async def test_operational_unreadable_or_unbounded_probe_fails_closed(payload):
    async def probe(argv):
        return payload
    with pytest.raises(ValueError):
        await operations().verify_local_operations(operation_inventory(), runner=probe, environ={})


@pytest.mark.integration
@pytest.mark.parametrize("drift", ["freshness", "operations"])
async def test_prepare_rechecks_freshness_and_operations_after_capture(archive_db, tmp_path, monkeypatch, drift):
    from types import SimpleNamespace

    factory, kwargs = await prepare_fixture(archive_db)
    original = cli().capture_archive
    clock = [0.0]
    monkeypatch.setattr(cli(), "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def delayed_capture(*args, **options):
        manifest = await original(*args, **options)
        if drift == "freshness":
            clock[0] = 301.0
        else:
            kwargs["runner"].running = True
        return manifest

    monkeypatch.setattr(cli(), "capture_archive", delayed_capture)
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **kwargs, output=tmp_path / "prepared", dry_run=False)
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 0


@pytest.mark.integration
async def test_omitted_noinherit_login_with_reachable_writer_role_blocks_prepare(archive_db, tmp_path):
    from uuid import uuid4

    factory, kwargs = await prepare_fixture(archive_db)
    suffix = uuid4().hex
    with archive_db[1].begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE writer_{suffix} NOLOGIN")
        connection.exec_driver_sql(f"CREATE ROLE omitted_{suffix} LOGIN NOINHERIT")
        connection.exec_driver_sql(f"GRANT writer_{suffix} TO omitted_{suffix}")
        connection.exec_driver_sql(f"GRANT UPDATE ON position_state TO writer_{suffix}")
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **kwargs, output=tmp_path / "prepared", dry_run=False)


@pytest.mark.integration
async def test_prepare_refuses_new_event_since_reviewed_repeatable_read_snapshot(archive_db, tmp_path):
    factory, kwargs = await prepare_fixture(archive_db)
    async with factory.begin() as session:
        await session.execute(text(
            "INSERT INTO event_log(account_id,exchange_account_id,deployment_environment,"
            "event_type,payload,occurred_at_ms) VALUES (:s,:id,'ci','CREDIT_CLOSED','{}',1300)"
        ), {"id": UUID(int=100), "s": str(UUID(int=100))})
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **kwargs, output=tmp_path / "prepared", dry_run=False)
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM event_log")) == 1


@pytest.mark.integration
async def test_prepare_refuses_empty_table_schema_drift_since_diagnose(archive_db, tmp_path):
    factory, kwargs = await prepare_fixture(archive_db)
    with archive_db[1].begin() as connection:
        connection.exec_driver_sql("ALTER TABLE venue_credit_state ADD COLUMN synthetic_drift text")
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **kwargs, output=tmp_path / "prepared", dry_run=False)


async def test_real_parser_nonempty_offer_and_credit_records_and_safe_failure():
    import httpx

    from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
    from tests.external.bitfinex.test_auth_rest import _row
    from tests.modules.execution.projection_cutover.test_snapshot import SCOPE, SYMBOLS

    response_error = False

    def respond(request):
        if response_error:
            return httpx.Response(500, text="SYNTHETIC-SECRET-RAW-ERROR")
        if request.url.path.endswith("offers"):
            return httpx.Response(200, json=[_row(offer_id=1, symbol="fUST", mts=1000, amount=-1.25)])
        if request.url.path.endswith("credits"):
            return httpx.Response(200, json=[[2, "fUSD", 1, 1000, 1000, "2.5", 0, "ACTIVE", 0, "0.0002", 2]])
        return httpx.Response(200, json=[["funding", "UST", "1", "0", "1"], ["funding", "USD", "0", "0", "0"]])

    kwargs = {
        "scope": SCOPE, "managed_symbols": SYMBOLS, "max_age_ms": 300_000,
        "ctx": AccountContext(account_id=str(SCOPE.account_id),
                              credentials=Credentials(api_key="synthetic", api_secret="synthetic"),
                              allocation_cap_usdt=Decimal("0")),
        "transport": httpx.MockTransport(respond),
    }
    observed = await cli().collect_snapshot(**kwargs)
    assert observed.offers[0].amount_remaining == Decimal("1.25")
    assert observed.offers[0].rate == Decimal("0.00031")
    assert observed.credits[0].amount == Decimal("2.5")
    assert observed.credits[0].rate == Decimal("0.0002")
    response_error = True
    with pytest.raises(ValueError) as failure:
        await cli().collect_snapshot(**kwargs)
    assert "SECRET" not in str(failure.value)


@pytest.mark.integration
async def test_database_quiescence_refuses_hidden_session_metadata(archive_db):
    from uuid import uuid4

    factory, kwargs = await prepare_fixture(archive_db)
    reader = "synthetic_observer_" + uuid4().hex
    with archive_db[1].begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {reader} NOLOGIN")
        connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {reader}")
        connection.exec_driver_sql(f"GRANT SELECT ON trading_halt TO {reader}")
    async with factory() as other, factory() as observer:
        other_pid = await other.scalar(text("SELECT pg_backend_pid()"))
        await observer.execute(text(f"SET LOCAL ROLE {reader}"))
        metadata = (await observer.execute(text(
            "SELECT backend_type,state FROM pg_stat_activity WHERE pid=:pid"
        ), {"pid": other_pid})).one()
        assert metadata.backend_type is None and metadata.state is None
        with pytest.raises(ValueError):
            await operations().verify_database_quiescence(
                observer, scope=kwargs["scope"], runtime_roles=kwargs["runtime_roles"],
            )


@pytest.mark.integration
async def test_database_quiescence_refreshes_activity_after_new_writer_in_same_transaction(archive_db):
    from sqlalchemy.ext.asyncio import create_async_engine

    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter

    factory, kwargs = await prepare_fixture(archive_db)
    new_engine = create_async_engine(factory.kw["bind"].url)
    try:
        async with factory() as observer:
            await AccountEventWriter(store=PostgresEventStore(deployment_environment="ci")).acquire_lock(
                observer, account_id=kwargs["scope"].account_id,
            )
            await operations().verify_database_quiescence(
                observer, scope=kwargs["scope"], runtime_roles=kwargs["runtime_roles"],
            )
            async with new_engine.connect() as writer:
                writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
                # Positive reproduction control: this observer's first activity
                # snapshot does not include the newly opened client transaction.
                assert await observer.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:pid)"
                ), {"pid": writer_pid}) is False
                with pytest.raises(ValueError):
                    await operations().verify_database_quiescence(
                        observer, scope=kwargs["scope"], runtime_roles=kwargs["runtime_roles"],
                    )
    finally:
        await new_engine.dispose()


async def prepare_fixture(archive_db):
    import hashlib
    from uuid import uuid4

    from bfx_funding_bot.modules.execution.projection_cutover.codec import row_digest
    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope
    from tests.modules.execution.projection_cutover.test_snapshot import snapshot

    factory, engine = archive_db
    scope = Scope(UUID(int=100), "ci")
    role = "synthetic_runtime_" + uuid4().hex
    with engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} LOGIN")
        connection.execute(text(
            "INSERT INTO trading_halt(account_id,exchange_account_id,deployment_environment,"
            "halted,reason,actor,created_at_ms) VALUES (:s,:a,'ci',true,'synthetic','operator',1000)"
        ), {"s": str(scope.account_id), "a": scope.account_id})
    diag = await cli().diagnose(
        factory, scope=scope, run_id=UUID(int=99), image_digest="sha256:" + "a" * 64,
        projector_version="execution-state-v1",
    )
    payload = encode_row({
        "diagnostic_digest": row_digest(diag), "reviewer": "synthetic-operator",
        "classifications": [{**difference, "classification": "historical_state",
                              "reason": "synthetic projection has no event source",
                              "evidence": "empty-source-stream-fixture"}
                             for difference in diag["differences"]],
    })
    return factory, {
        "scope": scope, "run_id": UUID(int=99), "image_digest": "sha256:" + "a" * 64,
        "projector_version": "execution-state-v1", "diagnostic": diag,
        "classification_payload": payload, "classification_digest": hashlib.sha256(payload).hexdigest(),
        "snapshot": snapshot(), "managed_symbols": frozenset({"fUST", "fUSD"}),
        "now_ms": 1200, "max_age_ms": 300_000, "operation_inventory": operation_inventory(),
        "runtime_roles": (role,), "runner": FixedOperations(),
    }


@pytest.mark.integration
async def test_prepare_archives_only_repeat_verifies_identity_and_dry_run_rolls_back(archive_db, tmp_path):
    import hashlib

    factory, kwargs = await prepare_fixture(archive_db)
    output = tmp_path / "prepared"
    dry = await cli().prepare_archive(factory, **kwargs, output=output, dry_run=True)
    assert dry["kind"] == "projection-cutover-prepared-v1"
    assert not output.exists()
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 0
    result = await cli().prepare_archive(factory, **kwargs, output=output, dry_run=False)
    assert output.stat().st_mode & 0o777 == 0o600
    repeated = await cli().prepare_archive(
        factory, **kwargs, output=output, dry_run=False,
        prepared_digest=hashlib.sha256(output.read_bytes()).hexdigest(),
    )
    assert repeated == result
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 1
        assert await session.scalar(text("SELECT count(*) FROM event_log")) == 0
        assert await session.scalar(text("SELECT reserved FROM position_state WHERE symbol='fUST'")) == Decimal("1.2300")
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **{**kwargs, "image_digest": "sha256:" + "c" * 64},
                                    output=output, dry_run=False,
                                    prepared_digest=hashlib.sha256(output.read_bytes()).hexdigest())


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["projection", "halt", "superuser", "runtime_session"])
async def test_prepare_refuses_drift_or_nonquiescent_runtime(archive_db, tmp_path, mutation):
    factory, kwargs = await prepare_fixture(archive_db)
    other = None
    try:
        async with factory.begin() as session:
            if mutation == "projection":
                await session.execute(text("UPDATE position_state SET reserved=999"))
            elif mutation == "halt":
                await session.execute(text("UPDATE trading_halt SET halted=false"))
            elif mutation == "superuser":
                kwargs["runtime_roles"] = (await session.scalar(text("SELECT session_user")),)
        if mutation == "runtime_session":
            other = await factory.kw["bind"].connect()
            await other.execute(text(f"SET ROLE {kwargs['runtime_roles'][0]}"))
            # pg_stat_activity.usename is session_user, so a pending transaction
            # by another operator connection must also block capture.
        with pytest.raises(ValueError):
            await cli().prepare_archive(factory, **kwargs, output=tmp_path / "prepared", dry_run=False)
        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 0
    finally:
        if other is not None:
            await other.close()


def command_args(command, output):
    return [command, "--account-id", str(UUID(int=100)), "--environment", "ci",
            "--run-id", str(UUID(int=99)), "--image-digest", "sha256:" + "a" * 64,
            "--projector-version", "execution-state-v1", "--output", str(output)]


@pytest.mark.parametrize("flag", ["--account-id", "--environment", "--run-id", "--image-digest",
                                 "--projector-version", "--output"])
def test_cli_requires_every_explicit_identity_field(flag, tmp_path):
    args = command_args("diagnose", tmp_path / "evidence")
    position = args.index(flag)
    del args[position:position + 2]
    with pytest.raises(ValueError):
        cli().parse_args(args)


@pytest.mark.integration
async def test_cli_diagnose_and_independent_archive_verification(archive_db, tmp_path, monkeypatch):
    import hashlib

    from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row

    factory, kwargs = await prepare_fixture(archive_db)
    # Explicit password rendering is used only for this synthetic container env.
    monkeypatch.setenv("DATABASE_URL", factory.kw["bind"].url.render_as_string(hide_password=False))
    diagnostic_path = tmp_path / "diagnostic"
    result = await cli().run_command(cli().parse_args(command_args("diagnose", diagnostic_path)))
    assert result["status"] == "diagnosed"
    diagnostic_evidence = decode_row(diagnostic_path.read_bytes())
    assert diagnostic_evidence["stream"]["count"] == 0
    output = tmp_path / "prepared"
    await cli().prepare_archive(factory, **kwargs, output=output, dry_run=False)
    args = [*command_args("verify-archive", output),
        "--prepared-digest", hashlib.sha256(output.read_bytes()).hexdigest(),
    ]
    result = await cli().run_command(cli().parse_args(args))
    assert result["status"] == "verified"
    assert set(result) == {"status", "manifest_digest"}
    output.chmod(0o644)
    with pytest.raises(ValueError):
        await cli().run_command(cli().parse_args(args))


@pytest.mark.integration
async def test_cli_prepare_uses_uuid_vault_and_real_reads_outside_transaction(archive_db, tmp_path, monkeypatch):
    import base64
    import hashlib
    from dataclasses import asdict
    from datetime import UTC, datetime

    import httpx

    from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccountCredential

    factory, kwargs = await prepare_fixture(archive_db)
    monkeypatch.setenv("DATABASE_URL", factory.kw["bind"].url.render_as_string(hide_password=False))
    kek = bytes(range(32))
    monkeypatch.setenv("BFX_VAULT_KEK", base64.b64encode(kek).decode())
    envelope = encrypt_secret_with_aad("synthetic-secret", aad=str(UUID(int=100)), kek=kek)
    async with factory.begin() as session:
        session.add(ExchangeAccountCredential(
            exchange_account_id=UUID(int=100), venue="bitfinex", label="synthetic",
            api_key="synthetic-key", **asdict(envelope), lifecycle_status="active",
            verified_at=datetime.now(UTC),
        ))
    inventory = {**kwargs["operation_inventory"], "runtime_roles": dict.fromkeys(("bot", "webapi", "frontend", "weekly-report"), kwargs["runtime_roles"][0])}
    args = command_args("prepare", tmp_path / "prepared")
    for name, payload in (("diagnostic", encode_row(kwargs["diagnostic"])),
                          ("classification", kwargs["classification_payload"]),
                          ("operations", encode_row(inventory))):
        path = tmp_path / name
        cli().write_private(path, payload)
        args.extend([f"--{name}", str(path), f"--{name}-digest", hashlib.sha256(payload).hexdigest()])
    paths = []

    async def respond(request):
        paths.append(request.url.path)
        assert request.headers["bfx-apikey"] == "synthetic-key"
        async with factory() as session:
            # No credential or capture transaction remains open during venue IO.
            assert await session.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND backend_type='client backend' AND xact_start IS NOT NULL"
            )) == 0
        if request.url.path.endswith("wallets"):
            return httpx.Response(200, json=[["funding", "UST", "1", "0", "1"], ["funding", "USD", "0", "0", "0"]])
        return httpx.Response(200, json=[])

    result = await cli().run_command(cli().parse_args(args), runner=FixedOperations(), transport=httpx.MockTransport(respond))
    assert result["status"] == "prepared"
    assert paths == ["/v2/auth/r/funding/offers", "/v2/auth/r/funding/credits", "/v2/auth/r/wallets"]
    evidence = (tmp_path / "prepared").read_bytes()
    assert b"synthetic-secret" not in evidence and b"synthetic-key" not in evidence
    # A repeated prepare must use the identical independent snapshot/evidence,
    # not create a new venue query identity or recapture the immutable run.
    repeated = await cli().run_command(
        cli().parse_args([*args, "--prepared-digest", hashlib.sha256(evidence).hexdigest()]),
        runner=FixedOperations(), transport=httpx.MockTransport(respond),
    )
    assert repeated == result
    assert len(paths) == 3
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM event_log")) == 0
