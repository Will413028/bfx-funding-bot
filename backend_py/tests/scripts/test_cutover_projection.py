"""Prepare evidence must share the archive's lossless PostgreSQL representation."""

import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.execution.projection_cutover.codec import JSON_NULL, encode_row
from bfx_funding_bot.modules.execution.projection_cutover.evidence import (
    EvidenceWriter,
    VerifiedCutoverEvidence,
)
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


def _v2_identity():
    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope, StreamIdentity

    return {
        "run_id": UUID(int=99),
        "scope": Scope(UUID(int=100), "ci"),
        "image_digest": "sha256:" + "a" * 64,
        "projector_version": "execution-state-v1",
        "stream": StreamIdentity(2, 2, "b" * 64),
    }


def _v2_difference(index: int) -> dict[str, object]:
    return {
        "table": "position_state",
        "key_digest": f"{index + 3:064x}",
        "column": "last_updated_ms" if index == 0 else "reserved",
        "before_digest": "d" * 64,
        "after_digest": "e" * 64,
        "classification": "unexplained",
    }


def _write_v2_evidence(tmp_path, *, classifications=None, diagnostic_identity=None):
    identity = {**_v2_identity(), **(diagnostic_identity or {})}
    diagnostics = [_v2_difference(0), _v2_difference(1)]
    diagnostic_path = tmp_path / "diagnostic"
    diagnostic = EvidenceWriter.create(
        diagnostic_path,
        manifest_fields={
            "kind": "projection-cutover-diagnostic-v2",
            "format_version": 2,
            **identity,
            "record_kind": "difference",
            "tables": [],
        },
    )
    for record in diagnostics:
        diagnostic.append(record)
    diagnostic_manifest = diagnostic.finish()
    if classifications is None:
        classifications = [
            {
                **record,
                "classification": "historical_state" if index == 0 else "time_sequence",
                "reason": "synthetic reason",
                "evidence": "synthetic evidence",
            }
            for index, record in enumerate(diagnostics)
        ]
    classification_path = tmp_path / "classification"
    classification = EvidenceWriter.create(
        classification_path,
        manifest_fields={
            "kind": "projection-cutover-classification-v2",
            "format_version": 2,
            **identity,
            "record_kind": "classification",
            "diagnostic_digest": diagnostic_manifest.digest,
            "reviewer": "synthetic-operator",
        },
    )
    for record in classifications:
        classification.append(record)
    classification_manifest = classification.finish()
    return identity, diagnostics, diagnostic_manifest, classification_path, classification_manifest, diagnostic_path


def test_v2_verifier_returns_only_compact_lockstep_summary_and_exact_counts(tmp_path):
    identity, _, diagnostic_manifest, classification_path, classification_manifest, diagnostic_path = (
        _write_v2_evidence(tmp_path)
    )
    summary = cli().verify_cutover_evidence(
        diagnostic_path,
        classification_path,
        expected_diagnostic_digest=diagnostic_manifest.digest,
        expected_classification_digest=classification_manifest.digest,
        expected_run_id=identity["run_id"],
        expected_scope=identity["scope"],
        expected_image_digest=identity["image_digest"],
        expected_projector_version=identity["projector_version"],
    )
    assert isinstance(summary, VerifiedCutoverEvidence)
    assert summary.diagnostic == diagnostic_manifest
    assert summary.classification == classification_manifest
    assert summary.classification_counts == (("historical_state", 1), ("time_sequence", 1))
    assert not hasattr(summary, "records")


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "reordered", "unexplained", "unknown", "reason", "evidence"])
def test_v2_verifier_rejects_incomplete_or_unbound_classification(tmp_path, mutation):
    _, diagnostics, diagnostic_manifest, _, _, diagnostic_path = _write_v2_evidence(tmp_path)
    if mutation == "duplicate":
        records = [{**diagnostics[0], "classification": "historical_state", "reason": "r", "evidence": "e"}] * 2
    elif mutation == "missing":
        records = []
    elif mutation == "reordered":
        records = [
            {**diagnostics[1], "classification": "historical_state", "reason": "r", "evidence": "e"},
            {**diagnostics[0], "classification": "time_sequence", "reason": "r", "evidence": "e"},
        ]
    else:
        records = [
            {**diagnostics[0], "classification": "historical_state", "reason": "r", "evidence": "e"}
        ]
        records[0][mutation] = {
            "unexplained": "unexplained",
            "unknown": "not-an-allowed-class",
            "reason": "",
            "evidence": "",
        }[mutation]
    classification_path = tmp_path / "classification-mutated"
    classification = EvidenceWriter.create(
        classification_path,
        manifest_fields={
            "kind": "projection-cutover-classification-v2",
            "format_version": 2,
            **_v2_identity(),
            "record_kind": "classification",
            "diagnostic_digest": diagnostic_manifest.digest,
            "reviewer": "synthetic-operator",
        },
    )
    for record in records:
        classification.append(record)
    classification_manifest = classification.finish()
    with pytest.raises(ValueError):
        cli().verify_cutover_evidence(
            diagnostic_path,
            classification_path,
            expected_diagnostic_digest=diagnostic_manifest.digest,
            expected_classification_digest=classification_manifest.digest,
            expected_run_id=_v2_identity()["run_id"],
            expected_scope=_v2_identity()["scope"],
            expected_image_digest=_v2_identity()["image_digest"],
            expected_projector_version=_v2_identity()["projector_version"],
        )


def test_v2_verifier_rejects_identity_digest_and_incomplete_directory(tmp_path):
    identity, _, diagnostic_manifest, classification_path, classification_manifest, diagnostic_path = (
        _write_v2_evidence(tmp_path)
    )
    with pytest.raises(ValueError):
        cli().verify_cutover_evidence(
            diagnostic_path,
            classification_path,
            expected_diagnostic_digest="0" * 64,
            expected_classification_digest=classification_manifest.digest,
            expected_run_id=identity["run_id"],
            expected_scope=identity["scope"],
            expected_image_digest=identity["image_digest"],
            expected_projector_version=identity["projector_version"],
        )
    with pytest.raises(ValueError):
        cli().verify_cutover_evidence(
            diagnostic_path,
            classification_path,
            expected_diagnostic_digest=diagnostic_manifest.digest,
            expected_classification_digest=classification_manifest.digest,
            expected_run_id=UUID(int=101),
            expected_scope=identity["scope"],
            expected_image_digest=identity["image_digest"],
            expected_projector_version=identity["projector_version"],
        )
    (classification_path / "COMPLETE").unlink()
    with pytest.raises(ValueError):
        cli().verify_cutover_evidence(
            diagnostic_path,
            classification_path,
            expected_diagnostic_digest=diagnostic_manifest.digest,
            expected_classification_digest=classification_manifest.digest,
            expected_run_id=identity["run_id"],
            expected_scope=identity["scope"],
            expected_image_digest=identity["image_digest"],
            expected_projector_version=identity["projector_version"],
        )


def test_v2_verifier_rejects_non_string_classification(tmp_path):
    _, diagnostics, diagnostic_manifest, _, _, diagnostic_path = _write_v2_evidence(tmp_path)
    classification_path = tmp_path / "classification-unhashable"
    classification = EvidenceWriter.create(
        classification_path,
        manifest_fields={
            "kind": "projection-cutover-classification-v2",
            "format_version": 2,
            **_v2_identity(),
            "record_kind": "classification",
            "diagnostic_digest": diagnostic_manifest.digest,
            "reviewer": "synthetic-operator",
        },
    )
    classification.append({
        **diagnostics[0],
        "classification": ["historical_state"],
        "reason": "r",
        "evidence": "e",
    })
    classification.append({
        **diagnostics[1],
        "classification": "historical_state",
        "reason": "r",
        "evidence": "e",
    })
    classification_manifest = classification.finish()
    with pytest.raises(ValueError):
        cli().verify_cutover_evidence(
            diagnostic_path,
            classification_path,
            expected_diagnostic_digest=diagnostic_manifest.digest,
            expected_classification_digest=classification_manifest.digest,
            expected_run_id=UUID(int=99),
            expected_scope=_v2_identity()["scope"],
            expected_image_digest=_v2_identity()["image_digest"],
            expected_projector_version=_v2_identity()["projector_version"],
        )


def test_prepare_requires_v2_evidence_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite://")
    identity = _v2_identity()
    args = cli().parse_args([
        "prepare", "--account-id", str(identity["scope"].account_id), "--environment", "ci",
        "--run-id", str(identity["run_id"]), "--image-digest", identity["image_digest"],
        "--projector-version", identity["projector_version"], "--output", str(tmp_path / "prepared"),
        "--diagnostic", str(tmp_path / "legacy-diagnostic.json"), "--diagnostic-digest", "a" * 64,
        "--classification", str(tmp_path / "legacy-classification.json"), "--classification-digest", "b" * 64,
        "--operations", str(tmp_path / "operations"), "--operations-digest", "c" * 64,
    ])
    with pytest.raises(ValueError, match="evidence_format_invalid"):
        asyncio.run(cli().run_command(args))


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
async def test_diagnose_captures_all_original_columns_without_mutation(pg_session_factory, tmp_path):
    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope
    from bfx_funding_bot.modules.execution.projection_cutover.evidence import (
        DIAGNOSTIC_KIND,
        iter_verified_records,
    )
    from tests.integration.test_projection_cutover_diagnostics import _seed, _source_state
    from tests.modules.execution.event_store.test_historical_claim_cycles import ACCOUNT

    await _seed(pg_session_factory)
    before = await _source_state(pg_session_factory)
    result = await cli().diagnose(
        pg_session_factory, scope=Scope(ACCOUNT, "ci"), run_id=UUID(int=99),
        image_digest="sha256:" + "a" * 64, projector_version="execution-state-v1",
        output=tmp_path / "diagnostic",
    )
    assert result.kind == DIAGNOSTIC_KIND
    assert result.stream.count > 0
    assert len(result.tables) == 8
    differences = list(iter_verified_records(
        tmp_path / "diagnostic", expected_digest=result.digest, expected_kind=DIAGNOSTIC_KIND,
    ))
    assert all(d["classification"] == "unexplained" for d in differences)
    assert any(d["column"] == "last_updated_ms" for d in differences)
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


def _prepare_kwargs(values):
    return {
        key: value
        for key, value in values.items()
        if key not in {"diagnostic_path", "classification_path", "diagnostic_digest", "classification_digest"}
    }


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
        await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=tmp_path / "prepared", dry_run=False)
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
        await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=tmp_path / "prepared", dry_run=False)


@pytest.mark.integration
async def test_prepare_refuses_new_event_since_reviewed_repeatable_read_snapshot(archive_db, tmp_path):
    factory, kwargs = await prepare_fixture(archive_db)
    async with factory.begin() as session:
        await session.execute(text(
            "INSERT INTO event_log(account_id,exchange_account_id,deployment_environment,"
            "event_type,payload,occurred_at_ms) VALUES (:s,:id,'ci','CREDIT_CLOSED','{}',1300)"
        ), {"id": UUID(int=100), "s": str(UUID(int=100))})
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=tmp_path / "prepared", dry_run=False)
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM event_log")) == 1


@pytest.mark.integration
async def test_prepare_refuses_empty_table_schema_drift_since_diagnose(archive_db, tmp_path):
    factory, kwargs = await prepare_fixture(archive_db)
    with archive_db[1].begin() as connection:
        connection.exec_driver_sql("ALTER TABLE venue_credit_state ADD COLUMN synthetic_drift text")
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=tmp_path / "prepared", dry_run=False)


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
    from pathlib import Path
    from tempfile import mkdtemp
    from uuid import uuid4

    from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope
    from bfx_funding_bot.modules.execution.projection_cutover.evidence import (
        CLASSIFICATION_KIND,
        EvidenceWriter,
        iter_verified_records,
    )
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
    evidence_root = Path(mkdtemp(prefix="bfx-cutover-fixture-"))
    diagnostic_path = evidence_root / "diagnostic"
    diag = await cli().diagnose(
        factory, scope=scope, run_id=UUID(int=99), image_digest="sha256:" + "a" * 64,
        projector_version="execution-state-v1", output=diagnostic_path,
    )
    classification_path = evidence_root / "classification"
    classification = EvidenceWriter.create(
        classification_path,
        manifest_fields={
            "kind": CLASSIFICATION_KIND,
            "format_version": 2,
            "run_id": diag.run_id,
            "scope": diag.scope,
            "image_digest": diag.image_digest,
            "projector_version": diag.projector_version,
            "stream": diag.stream,
            "record_kind": "classification",
            "diagnostic_digest": diag.digest,
            "reviewer": "synthetic-operator",
        },
    )
    for difference in iter_verified_records(
        diagnostic_path, expected_digest=diag.digest, expected_kind="projection-cutover-diagnostic-v2",
    ):
        classification.append({
            **difference,
            "classification": "historical_state",
            "reason": "synthetic projection has no event source",
            "evidence": "empty-source-stream-fixture",
        })
    classification_manifest = classification.finish()
    verified = cli().verify_cutover_evidence(
        diagnostic_path,
        classification_path,
        expected_diagnostic_digest=diag.digest,
        expected_classification_digest=classification_manifest.digest,
        expected_run_id=diag.run_id,
        expected_scope=scope,
        expected_image_digest=diag.image_digest,
        expected_projector_version=diag.projector_version,
    )
    return factory, {
        "scope": scope, "run_id": UUID(int=99), "image_digest": "sha256:" + "a" * 64,
        "projector_version": "execution-state-v1", "evidence": verified,
        "diagnostic_path": diagnostic_path, "classification_path": classification_path,
        "diagnostic_digest": diag.digest, "classification_digest": classification_manifest.digest,
        "snapshot": snapshot(), "managed_symbols": frozenset({"fUST", "fUSD"}),
        "now_ms": 1200, "max_age_ms": 300_000, "operation_inventory": operation_inventory(),
        "runtime_roles": (role,), "runner": FixedOperations(),
    }


@pytest.mark.integration
@pytest.mark.parametrize("drift", [None, "value", "key", "scope", "count", "schema"])
async def test_prepare_streams_exact_facts_without_projection_lists(archive_db, tmp_path, monkeypatch, drift):
    import hashlib

    from bfx_funding_bot.modules.execution.projection_cutover.archive import verify_archive
    from bfx_funding_bot.modules.execution.projection_cutover.manifest import decode_manifest
    from tests.integration.test_projection_cutover_apply_v2 import complete_raw

    factory, kwargs = await prepare_fixture(archive_db)

    async def forbidden(*args, **kw):
        raise AssertionError("prepare materialized all projection tables")

    # This was prepare's whole-dataset reader. Neither first prepare nor repeat
    # may require it; the streaming path must still prove exact physical facts.
    monkeypatch.setattr(cli(), "_archive_projection_rows", forbidden, raising=False)
    if drift is not None:
        async with factory.begin() as session:
            await session.execute(text({
                "value": "UPDATE reconcile_observation SET recorded_at=recorded_at + interval '1 microsecond' WHERE deployment_environment='ci'",
                "key": "UPDATE reconcile_observation SET id=id+100 WHERE deployment_environment='ci'",
                "scope": "UPDATE reconcile_observation SET deployment_environment='shadow' WHERE deployment_environment='ci'",
                "count": "DELETE FROM reconcile_observation WHERE deployment_environment='ci'",
                "schema": "ALTER TABLE venue_credit_state ADD COLUMN synthetic_drift text",
            }[drift]))
    before = await complete_raw(factory)
    output = tmp_path / "prepared"
    if drift is not None:
        with pytest.raises(ValueError, match="prepare_original_projection_drift"):
            await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=output, dry_run=False)
        assert await complete_raw(factory) == before
        assert not output.exists()
        return

    result = await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=output, dry_run=False)
    manifest = decode_manifest(encode_row(result["manifest"]))
    assert manifest.tables == kwargs["evidence"].diagnostic.tables
    assert {entry["name"]: entry["count"] for entry in manifest.tables} == {
        "execution_uncertainties": 0, "offer_claims": 0, "position_state": 1,
        "projection_heads": 0, "reconcile_observation": 1, "submission_attempts": 0,
        "venue_credit_state": 0, "venue_offer_state": 0,
    }
    async with factory() as session:
        await verify_archive(session, expected=manifest)
    after = await complete_raw(factory)
    assert {name: rows for name, rows in after.items() if name.startswith("public.")} == {
        name: rows for name, rows in before.items() if name.startswith("public.")
    }
    assert await cli().prepare_archive(
        factory, **_prepare_kwargs(kwargs), output=output, dry_run=False,
        prepared_digest=hashlib.sha256(output.read_bytes()).hexdigest(),
    ) == result
    assert await complete_raw(factory) == after


@pytest.mark.integration
async def test_prepare_archives_only_repeat_verifies_identity_and_dry_run_rolls_back(archive_db, tmp_path):
    import hashlib

    factory, kwargs = await prepare_fixture(archive_db)
    output = tmp_path / "prepared"
    dry = await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=output, dry_run=True)
    assert dry["kind"] == "projection-cutover-prepared-v1"
    assert not output.exists()
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 0
    result = await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=output, dry_run=False)
    assert output.stat().st_mode & 0o777 == 0o600
    from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row

    prepared_payload = output.read_bytes()
    prepared = decode_row(prepared_payload)
    assert prepared["diagnostic_digest"] == kwargs["evidence"].diagnostic.digest
    assert prepared["classification_digest"] == kwargs["evidence"].classification.digest
    assert b"chunks" not in prepared_payload
    assert b"synthetic projection has no event source" not in prepared_payload
    assert b"empty-source-stream-fixture" not in prepared_payload
    repeated = await cli().prepare_archive(
        factory, **_prepare_kwargs(kwargs), output=output, dry_run=False,
        prepared_digest=hashlib.sha256(output.read_bytes()).hexdigest(),
    )
    assert repeated == result
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 1
        assert await session.scalar(text("SELECT count(*) FROM event_log")) == 0
        assert await session.scalar(text("SELECT reserved FROM position_state WHERE symbol='fUST'")) == Decimal("1.2300")
    with pytest.raises(ValueError):
        await cli().prepare_archive(factory, **_prepare_kwargs({**kwargs, "image_digest": "sha256:" + "c" * 64}),
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
            await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=tmp_path / "prepared", dry_run=False)
        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM projection_audit.runs")) == 0
    finally:
        if other is not None:
            await other.close()


def command_args(command, output):
    return [command, "--account-id", str(UUID(int=100)), "--environment", "ci",
            "--run-id", str(UUID(int=99)), "--image-digest", "sha256:" + "a" * 64,
            "--projector-version", "execution-state-v1", "--output", str(output)]


@pytest.mark.integration
@pytest.mark.parametrize("lost_output", [False, True])
async def test_cli_apply_verifies_all_io_before_lock_and_repeat_without_http(archive_db, tmp_path, monkeypatch, lost_output):
    import hashlib
    import json

    from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
    from tests.integration.test_projection_cutover_apply_v2 import complete_raw, fixture

    factory, runtime_args, values = await fixture(archive_db, tmp_path)
    module = cli()
    monkeypatch.setenv("DATABASE_URL", factory.kw["bind"].url.render_as_string(hide_password=False))
    monkeypatch.setattr(module.time, "time", lambda: 1.5)
    args = command_args("apply", tmp_path / "applied")
    files = {
        "prepared": (tmp_path / "prepared").read_bytes(),
        "receipt": json.dumps(runtime_args["archive_restore_receipt"]).encode(),
        "archive-input": values["archive_input"],
        "operations": encode_row(values["operation_inventory"]),
        "snapshot": encode_row(serialize_event(runtime_args["snapshot"])),
    }
    for flag, payload in files.items():
        path = tmp_path / flag
        if flag != "prepared":
            module.write_private(path, payload)
        args.extend(["--" + flag, str(path), "--" + flag + "-digest", hashlib.sha256(payload).hexdigest()])
    args.extend(["--diagnostic", str(values["diagnostic_path"]), "--diagnostic-digest", values["diagnostic_digest"],
                 "--classification", str(values["classification_path"]), "--classification-digest", values["classification_digest"]])
    lock_taken = False
    reads = set()
    original_read = module.read_private
    original_verify = module.verify_cutover_evidence
    original_lock = AccountEventWriter.acquire_lock

    def read(path, **kw):
        if path.name != "applied":
            assert not lock_taken
            reads.add(path.name)
        return original_read(path, **kw)

    def verify(*a, **kw):
        assert not lock_taken
        result = original_verify(*a, **kw)
        reads.add("v2-complete")
        return result

    async def lock(self, session, **kw):
        nonlocal lock_taken
        assert reads == {*files, "v2-complete"}
        lock_taken = True
        return await original_lock(self, session, **kw)

    async def no_http(**kw):
        raise AssertionError("apply must use the pinned snapshot and must never query the venue")

    monkeypatch.setattr(module, "read_private", read)
    monkeypatch.setattr(module, "verify_cutover_evidence", verify)
    monkeypatch.setattr(module, "collect_snapshot", no_http)
    monkeypatch.setattr(AccountEventWriter, "acquire_lock", lock)
    if lost_output:
        original_write = module.write_private
        def fail_output(*a, **kw):
            raise OSError("synthetic post-commit output failure")
        monkeypatch.setattr(module, "write_private", fail_output)
        with pytest.raises(OSError, match="post-commit"):
            await module.run_command(module.parse_args(args), runner=values["runner"])
        durable = await complete_raw(factory)
        assert len(durable["public.event_log"]) == len(durable["projection_audit.receipts"]) == 1
        assert json.loads(durable["public.trading_halt"][0])["halted"] is True
        assert not (tmp_path / "applied").exists()
        monkeypatch.setattr(module, "write_private", original_write)
        lock_taken = False
        reads.clear()
        monkeypatch.setattr(module.time, "time", lambda: 999999)
    result = await module.run_command(module.parse_args(args), runner=values["runner"])
    assert result["status"] == "applied"
    committed = await complete_raw(factory)
    lock_taken = False
    reads.clear()
    monkeypatch.setattr(module.time, "time", lambda: 999999)
    assert await module.run_command(module.parse_args(args), runner=values["runner"]) == result
    assert await complete_raw(factory) == committed


def test_private_reader_enforces_command_bound_before_reading(tmp_path, monkeypatch):
    import hashlib
    import os

    module = cli()
    path = tmp_path / "bounded"
    module.write_private(path, b"a" * 33)
    original = os.fdopen
    class Unreadable:
        def __init__(self, descriptor, mode):
            self.source = original(descriptor, mode)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.source.close()
        def fileno(self):
            return self.source.fileno()
        def read(self, *args):
            raise AssertionError("oversized file was read")
    monkeypatch.setattr(os, "fdopen", Unreadable)
    with pytest.raises(ValueError, match="evidence_file"):
        module.read_private(path, expected_digest=hashlib.sha256(b"a" * 33).hexdigest(), max_bytes=32)


@pytest.mark.parametrize("mutation", [
    "receipt_digest", "prepared_pin", "target", "v1", "operations", "diagnostic",
    "snapshot_scope", "snapshot_environment", "snapshot_coverage", "snapshot_wallet_symbols",
    "snapshot_wallet_infinite", "snapshot_offer_original_infinite", "snapshot_offer_rate_infinite",
    "snapshot_credit_amount_infinite",
])
@pytest.mark.integration
async def test_cli_apply_rejects_unverified_files_before_transaction(archive_db, tmp_path, monkeypatch, mutation):
    import hashlib
    import json
    from dataclasses import replace

    from bfx_funding_bot.modules.execution.event_store.entities import (
        VenueCreditObservation,
        VenueOfferObservation,
    )
    from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
    from tests.integration.test_projection_cutover_apply_v2 import fixture

    _, runtime_args, values = await fixture(archive_db, tmp_path)
    module = cli()
    receipt = runtime_args["archive_restore_receipt"]
    if mutation == "prepared_pin":
        receipt["archive_verification"]["archives"][0]["prepared_digest"] = "0" * 64
    if mutation == "target":
        receipt["target_run_id"] = str(UUID(int=123))
    if mutation == "v1":
        receipt["schema_version"] = 1
    snapshot = serialize_event(replace(runtime_args["snapshot"],
        offers=(VenueOfferObservation("o1", "fUST", Decimal("9"), Decimal("7"),
                                      Decimal("0.001"), 2, "active", 900, 1000),),
        credits=(VenueCreditObservation("c1", "fUSD", Decimal("11"), Decimal("0.002"),
                                        3, "active", 900, 1000),)))
    if mutation == "snapshot_scope":
        snapshot["account_id"] = str(UUID(int=101))
    elif mutation == "snapshot_environment":
        snapshot["environment"] = "prod"
    elif mutation == "snapshot_coverage":
        snapshot["coverage"]["active_credits_complete"] = False
    elif mutation == "snapshot_wallet_symbols":
        del snapshot["wallet_available"]["fUSD"]
    elif mutation == "snapshot_wallet_infinite":
        snapshot["wallet_available"]["fUSD"] = "Infinity"
    elif mutation == "snapshot_offer_original_infinite":
        snapshot["offers"][0]["amount_original"] = "Infinity"
    elif mutation == "snapshot_offer_rate_infinite":
        snapshot["offers"][0]["rate"] = "Infinity"
    elif mutation == "snapshot_credit_amount_infinite":
        snapshot["credits"][0]["amount"] = "Infinity"
    args = command_args("apply", tmp_path / "applied")
    files = {"prepared": (tmp_path / "prepared").read_bytes(),
             "receipt": json.dumps(receipt).encode(), "archive-input": values["archive_input"],
             "operations": encode_row(values["operation_inventory"]),
             "snapshot": encode_row(snapshot)}
    for flag, payload in files.items():
        path = tmp_path / flag
        if flag != "prepared":
            module.write_private(path, payload)
        pin = "0" * 64 if mutation == "receipt_digest" and flag == "receipt" else hashlib.sha256(payload).hexdigest()
        args.extend(["--" + flag, str(path), "--" + flag + "-digest", pin])
    args.extend(["--diagnostic", str(values["diagnostic_path"]),
                 "--diagnostic-digest", "0" * 64 if mutation == "diagnostic" else values["diagnostic_digest"],
                 "--classification", str(values["classification_path"]), "--classification-digest", values["classification_digest"]])
    if mutation == "operations":
        (tmp_path / "operations").chmod(0o644)

    def forbidden(*a, **kw):
        raise AssertionError("unverified artifacts reached DB creation")

    monkeypatch.setattr(module, "make_async_engine_from_url", forbidden)
    with pytest.raises(ValueError):
        await module.run_command(module.parse_args(args), runner=values["runner"])


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

    from bfx_funding_bot.modules.execution.projection_cutover.evidence import (
        DIAGNOSTIC_KIND,
        iter_verified_records,
        verify_artifact,
    )

    factory, kwargs = await prepare_fixture(archive_db)
    # Explicit password rendering is used only for this synthetic container env.
    monkeypatch.setenv("DATABASE_URL", factory.kw["bind"].url.render_as_string(hide_password=False))
    diagnostic_path = tmp_path / "diagnostic"
    result = await cli().run_command(cli().parse_args(command_args("diagnose", diagnostic_path)))
    assert result["status"] == "diagnosed"
    diagnostic_manifest = verify_artifact(
        diagnostic_path,
        expected_digest=result["diagnostic_digest"],
        expected_kind=DIAGNOSTIC_KIND,
    )
    assert diagnostic_manifest.stream.count == 0
    assert result["difference_count"] == diagnostic_manifest.record_count
    assert all(
        record["classification"] == "unexplained"
        for record in iter_verified_records(
            diagnostic_path,
            expected_digest=diagnostic_manifest.digest,
            expected_kind=DIAGNOSTIC_KIND,
        )
    )
    output = tmp_path / "prepared"
    await cli().prepare_archive(factory, **_prepare_kwargs(kwargs), output=output, dry_run=False)
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
    args.extend([
        "--diagnostic", str(kwargs["diagnostic_path"]),
        "--diagnostic-digest", kwargs["diagnostic_digest"],
        "--classification", str(kwargs["classification_path"]),
        "--classification-digest", kwargs["classification_digest"],
    ])
    operations_path = tmp_path / "operations"
    operations_payload = encode_row(inventory)
    cli().write_private(operations_path, operations_payload)
    args.extend([
        "--operations", str(operations_path),
        "--operations-digest", hashlib.sha256(operations_payload).hexdigest(),
    ])
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
