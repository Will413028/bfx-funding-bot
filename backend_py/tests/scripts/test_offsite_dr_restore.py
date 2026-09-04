"""Offline contracts for the isolated offsite restore drill."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMMANDS_PATH = ROOT / "deploy/vm/pgbackrest/restore_commands.py"
DRILL_PATH = ROOT / "deploy/vm/pgbackrest/restore_drill.py"
EVIDENCE_PATH = ROOT / "deploy/vm/pgbackrest/evidence.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


restore_commands = _load_module("offsite_dr_restore_commands", COMMANDS_PATH)
restore_drill = _load_module("offsite_dr_restore_drill", DRILL_PATH)
evidence = _load_module("offsite_dr_evidence_for_restore", EVIDENCE_PATH)
RestoreInputError = restore_commands.RestoreInputError
build_restore_plan = restore_commands.build_restore_plan
DrillRequest = restore_drill.DrillRequest
RestoreDrill = restore_drill.RestoreDrill
EvidenceError = evidence.EvidenceError
render_restore_evidence = evidence.render_restore_evidence


def _replay_report(*, matches: bool = True) -> str:
    projection_names = (
        "offer_claims",
        "position_state",
        "venue_offer_state",
        "venue_credit_state",
        "projection_heads",
        "reconcile_observation",
        "submission_attempts",
        "execution_uncertainties",
    )
    row_counts: dict[str, int] = {"event_log": 9}
    content_hashes: dict[str, str] = {"event_log": "a" * 64}
    diagnostic_diff: dict[str, dict[str, int | str | bool]] = {}
    for index, name in enumerate(projection_names, start=1):
        digest = f"{index:x}" * 64
        row_counts[name] = index
        content_hashes[name] = digest
        diagnostic_diff[name] = {
            "old_count": index,
            "replayed_count": index,
            "old_hash": digest,
            "replayed_hash": digest,
            "matches": matches if name == projection_names[0] else True,
        }
    return json.dumps(
        {
            "account_id": "3f19d046-5030-494c-9a0a-9573bb890c1f",
            "environment": "prod",
            "projector_version": "projector-v3",
            "event_head": 42,
            "event_hash": "a" * 64,
            "row_counts": row_counts,
            "content_hashes": content_hashes,
            "diagnostic_diff": diagnostic_diff,
        }
    )


def test_dr_compose_is_internal_and_has_no_production_env_files() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.dr.yml").read_text())

    assert set(compose["services"]) == {"restore-db", "verifier"}
    assert compose["networks"]["dr"]["internal"] is True
    assert "ports" not in compose["services"]["restore-db"]
    assert "ports" not in compose["services"]["verifier"]
    source = (ROOT / "docker-compose.dr.yml").read_text()
    for forbidden in ("bot.env", "webapi.env", "frontend.env", ".env.runtime", "BFX_VAULT_KEK"):
        assert forbidden not in source


def test_restore_plan_names_are_random_prefixed_and_cleanup_is_generated_only() -> None:
    plan = build_restore_plan(
        account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
        environment="prod",
        projector_version="projector-v3",
        backup_label="20260904031700-F",
        target_time=None,
        run_id="20260904T031700Z-a1b2c3d4e5f60718",
    )

    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.volume_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.network_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.container_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.project_name)
    assert all(
        argument.startswith("bfx-dr-")
        for command in plan.cleanup_commands
        for argument in command
        if argument.startswith("bfx-")
    )


def test_restore_plan_rejects_command_injection_and_noncanonical_account() -> None:
    with pytest.raises(RestoreInputError):
        build_restore_plan(
            account_id="not-a-uuid",
            environment="prod",
            projector_version="v3",
            backup_label="20260904031700-F;rm",
            target_time=None,
            run_id="20260904T031700Z-a1b2c3d4e5f60718",
        )

    with pytest.raises(RestoreInputError):
        build_restore_plan(
            account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
            environment="prod",
            projector_version="v3",
            backup_label="20260904031700-F;rm",
            target_time=None,
            run_id="20260904T031700Z-a1b2c3d4e5f60718",
        )


def test_restore_evidence_contains_only_validated_measurements(tmp_path: Path) -> None:
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")

    report = render_restore_evidence(
        schema_tsv="180000\thead-a,head-b\t9",
        replay_json=_replay_report(),
        target_backup_label="20260904031700-F",
        elapsed_seconds=37,
        config_path=config,
        image_digest=f"sha256:{'b' * 64}",
        network_name="bfx-dr-20260904t031700z-a1b2c3d4e5f60718",
        network_internal=True,
    )

    assert report["measured"] is True
    assert report["rto_seconds"] == 37
    assert report["event_hash"] == "a" * 64
    assert report["row_counts"]["offer_claims"] == 1
    assert report["projection_hashes"]["offer_claims"] == "1" * 64
    assert report["network_internal"] is True
    assert report["config_digest"]
    assert report["image_digest"] == f"sha256:{'b' * 64}"


def test_restore_evidence_rejects_a_false_projection_diagnostic(tmp_path: Path) -> None:
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match=r"^projection_replay_mismatch$"):
        render_restore_evidence(
            schema_tsv="180000\thead-a\t9",
            replay_json=_replay_report(matches=False),
            target_backup_label="20260904031700-F",
            elapsed_seconds=37,
            config_path=config,
            image_digest=f"sha256:{'b' * 64}",
            network_name="bfx-dr-20260904t031700z-a1b2c3d4e5f60718",
            network_internal=True,
        )


class _FakeRunner:
    def __init__(self, *, verifier: subprocess.CompletedProcess[str], network_internal: str = "true\n") -> None:
        self.commands: list[tuple[str, ...]] = []
        self.env_text = ""
        self.env_mode: int | None = None
        self.verifier = verifier
        self.network_internal = network_internal

    def __call__(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if "--env-file" in command:
            env_path = Path(command[command.index("--env-file") + 1])
            self.env_text = env_path.read_text(encoding="utf-8")
            self.env_mode = env_path.stat().st_mode & 0o777
        if command[:3] == ("docker", "network", "inspect"):
            return subprocess.CompletedProcess(command, 0, self.network_internal, "")
        if command[:3] == ("docker", "inspect", "--format={{.State.Health.Status}}"):
            return subprocess.CompletedProcess(command, 0, "healthy\n", "")
        if command[:3] == ("docker", "exec", command[2]):
            return subprocess.CompletedProcess(command, 0, "180000\thead-a\t9\n", "")
        if "run" in command and "verifier" in command:
            return self.verifier
        if command[:4] == ("docker", "image", "inspect", "--format={{index .RepoDigests 0}}"):
            return subprocess.CompletedProcess(command, 0, f"sha256:{'b' * 64}\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")


def _drill(
    tmp_path: Path,
    fake: _FakeRunner,
    *,
    run_id: str = "20260904T031700Z-a1b2c3d4e5f60718",
) -> RestoreDrill:
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    return RestoreDrill(
        command_runner=fake,
        config_path=config,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: run_id,
        password_factory=lambda: "DATABASE-PASSWORD-SENTINEL",
    )


def _request() -> DrillRequest:
    return DrillRequest(
        account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
        environment="prod",
        projector_version="projector-v3",
        backup_label="20260904031700-F",
        target_time=None,
    )


def test_verifier_failure_cleans_only_generated_resources_and_redacts_secrets(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(
            ("fake",), 2, "TOKEN-SENTINEL", "DATABASE-PASSWORD-SENTINEL"
        )
    )
    drill = _drill(tmp_path, fake)

    assert drill.run(_request()) == 2

    report = (tmp_path / "restore.json").read_text(encoding="utf-8")
    log = (tmp_path / "restore.log").read_text(encoding="utf-8")
    assert json.loads(report)["measured"] is False
    assert json.loads(report)["error_code"] == "restore_command_failed"
    assert "TOKEN-SENTINEL" not in report + log
    assert "DATABASE-PASSWORD-SENTINEL" not in report + log
    assert fake.env_mode == 0o600
    assert {line.partition("=")[0] for line in fake.env_text.splitlines()} == {
        "DR_PROJECT_NAME",
        "DR_VOLUME_NAME",
        "DR_NETWORK_NAME",
        "DR_CONTAINER_NAME",
        "DR_POSTGRES_USER",
        "DR_POSTGRES_PASSWORD",
        "DR_POSTGRES_DB",
        "DATABASE_URL",
        "BFX_DEPLOYMENT_ENV",
        "DR_ACCOUNT_ID",
        "DR_PROJECTOR_VERSION",
        "DR_TARGET_BACKUP_LABEL",
        "DR_TARGET_TIME",
        "DR_PGBACKREST_SECRET_DIR",
    }
    cleanup = fake.commands[-3:]
    assert all(
        argument.startswith("bfx-dr-")
        for command in cleanup
        for argument in command
        if argument.startswith("bfx-")
    )


def test_noninternal_network_fails_before_restore_or_verifier(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        network_internal="false\n",
    )

    assert _drill(tmp_path, fake).run(_request()) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "network_not_internal"
    assert not any("verifier" in command for command in fake.commands)


def test_malformed_replay_hash_fails_without_a_success_measurement(tmp_path: Path) -> None:
    replay = json.loads(_replay_report())
    replay["event_hash"] = "not-a-hash"
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, json.dumps(replay), "")
    )

    assert _drill(tmp_path, fake).run(_request()) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "event_hash_invalid"


def test_command_runner_exception_is_redacted_into_failure_evidence(tmp_path: Path) -> None:
    def explode(_: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("TOKEN-SENTINEL")

    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    drill = RestoreDrill(
        command_runner=explode,
        config_path=config,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
    )

    assert drill.run(_request()) == 2

    report = (tmp_path / "restore.json").read_text(encoding="utf-8")
    assert json.loads(report)["error_code"] == "restore_command_failed"
    assert "TOKEN-SENTINEL" not in report
