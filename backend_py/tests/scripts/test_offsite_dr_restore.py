"""Offline contracts for the isolated offsite restore drill."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMMANDS_PATH = ROOT / "deploy/vm/pgbackrest/restore_commands.py"
DRILL_PATH = ROOT / "deploy/vm/pgbackrest/restore_drill.py"
EVIDENCE_PATH = ROOT / "deploy/vm/pgbackrest/evidence.py"
CONFIG_PATH = ROOT / "deploy/vm/pgbackrest/pgbackrest.conf"
ABSOLUTE_COMPOSE_PATH = str(ROOT / "docker-compose.dr.yml")


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


def _write_valid_secret_dir(tmp_path: Path) -> Path:
    secret_dir = tmp_path / "conf.d"
    secret_dir.mkdir()
    secret_file = secret_dir / "r2.conf"
    values = {
        "repo1-s3-endpoint": "https://account.r2.cloudflarestorage.com",
        "repo1-s3-bucket": "offsite-dr",
        "repo1-s3-key": "opaque-access-key",
        "repo1-s3-key-secret": "TOKEN-SENTINEL",
        "repo1-cipher-pass": "opaque-cipher-pass",
    }
    assignments = "\n".join(f"{key}={value}" for key, value in values.items())
    secret_file.write_text(f"[global]\n{assignments}\n", encoding="utf-8")
    secret_file.chmod(0o600)
    secret_dir.chmod(0o700)
    return secret_dir


def test_dr_compose_uses_generated_external_resources_without_production_inputs() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.dr.yml").read_text())

    assert set(compose["services"]) == {"restore-db", "verifier"}
    assert compose["volumes"] == {
        "restore-data": {"external": True, "name": "${DR_VOLUME_NAME}"}
    }
    assert compose["networks"] == {
        "dr": {"external": True, "name": "${DR_NETWORK_NAME}"},
        "r2-egress": {
            "external": True,
            "name": "${DR_EGRESS_NETWORK_NAME}",
        },
    }
    restore_db = compose["services"]["restore-db"]
    verifier = compose["services"]["verifier"]
    assert restore_db["networks"] == ["dr", "r2-egress"]
    assert verifier["networks"] == ["dr"]
    assert "restore-data:/var/lib/postgresql" in restore_db["volumes"]
    assert restore_db["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -U postgres -d template1",
    ]
    assert "ports" not in compose["services"]["restore-db"]
    assert "ports" not in compose["services"]["verifier"]
    source = (ROOT / "docker-compose.dr.yml").read_text()
    for forbidden in (
        "POSTGRES_",
        "bot.env",
        "webapi.env",
        "frontend.env",
        ".env.runtime",
        "BFX_VAULT_KEK",
    ):
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
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.egress_network_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.container_name)
    assert re.fullmatch(r"bfx-dr-[a-z0-9-]+", plan.project_name)
    assert plan.verify_role == "bfx_dr_20260904t031700z_a1b2c3d4e5f60718"
    assert plan.create_commands == (
        ("docker", "network", "create", "--internal", plan.network_name),
        ("docker", "network", "create", plan.egress_network_name),
        ("docker", "volume", "create", plan.volume_name),
    )
    assert sum(command.count("--internal") for command in plan.create_commands) == 1
    assert plan.run_commands[1] == (
        "docker",
        "network",
        "inspect",
        "--format={{.Internal}}",
        plan.egress_network_name,
    )
    assert plan.run_commands[2] == (
        "docker",
        "network",
        "disconnect",
        plan.egress_network_name,
        plan.container_name,
    )
    assert plan.run_commands[3] == (
        "docker",
        "inspect",
        "--format={{json .NetworkSettings.Networks}}",
        plan.container_name,
    )
    verifier_run_index = plan.run_commands[4].index("run")
    assert plan.run_commands[4][verifier_run_index : verifier_run_index + 5] == (
        "run",
        "--rm",
        "--no-deps",
        "verifier",
        "replay",
    )
    assert plan.cleanup_commands[1:] == (
        ("docker", "volume", "rm", plan.volume_name),
        ("docker", "network", "rm", plan.egress_network_name),
        ("docker", "network", "rm", plan.network_name),
    )
    assert all(isinstance(command, tuple) for command in (
        *plan.create_commands,
        *plan.run_commands,
        *plan.cleanup_commands,
    ))
    assert "bfx_pgdata" not in repr(plan)
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


def test_restore_plan_uses_an_absolute_compose_path_in_every_compose_argv() -> None:
    plan = build_restore_plan(
        account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
        environment="prod",
        projector_version="projector-v3",
        backup_label="20260904031700-F",
        target_time=None,
        run_id="20260904T031700Z-a1b2c3d4e5f60718",
    )

    compose_commands = [
        command
        for command in (*plan.run_commands, *plan.cleanup_commands)
        if command[:2] == ("docker", "compose")
    ]
    assert compose_commands
    assert all(ABSOLUTE_COMPOSE_PATH in command for command in compose_commands)


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
    def __init__(
        self,
        *,
        verifier: subprocess.CompletedProcess[str],
        network_internal: str = "true\n",
        egress_internal: str = "false\n",
        container_networks: str | None = None,
        compose_up_status: int = 0,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.env_text = ""
        self.env_mode: int | None = None
        self.verifier = verifier
        self.network_internal = network_internal
        self.egress_internal = egress_internal
        self.container_networks = container_networks
        self.compose_up_status = compose_up_status

    def __call__(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if "--env-file" in command:
            env_path = Path(command[command.index("--env-file") + 1])
            self.env_text = env_path.read_text(encoding="utf-8")
            self.env_mode = env_path.stat().st_mode & 0o777
        if command[:3] == ("docker", "network", "inspect"):
            output = self.egress_internal if command[-1].endswith("-egress") else self.network_internal
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[:3] == (
            "docker",
            "inspect",
            "--format={{json .NetworkSettings.Networks}}",
        ):
            networks = self.container_networks
            if networks is None:
                networks = json.dumps({"bfx-dr-20260904t031700z-a1b2c3d4e5f60718-net": {}})
            return subprocess.CompletedProcess(command, 0, f"{networks}\n", "")
        if "up" in command and "restore-db" in command:
            return subprocess.CompletedProcess(command, self.compose_up_status, "", "")
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
    clock: Callable[[], float] = restore_drill.time.monotonic,
) -> RestoreDrill:
    secret_dir = _write_valid_secret_dir(tmp_path)
    return RestoreDrill(
        command_runner=fake,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: run_id,
        password_factory=lambda: "DATABASE-PASSWORD-SENTINEL",
        clock=clock,
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )


def _request() -> DrillRequest:
    return DrillRequest(
        account_id="3f19d046-5030-494c-9a0a-9573bb890c1f",
        environment="prod",
        projector_version="projector-v3",
        backup_label="20260904031700-F",
        target_time=None,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _ImageAndEvidenceAdvancingRunner(_FakeRunner):
    def __init__(self, clock: _Clock) -> None:
        super().__init__(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
        self.clock = clock

    def __call__(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if command[:4] == ("docker", "image", "inspect", "--format={{index .RepoDigests 0}}"):
            self.clock.now += 1.25
        return super().__call__(command)


def test_rto_includes_image_and_evidence_validation_before_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    fake = _ImageAndEvidenceAdvancingRunner(clock)
    drill = _drill(tmp_path, fake, clock=clock)
    real_renderer = restore_drill.render_restore_evidence
    render_calls = 0

    def advancing_renderer(**kwargs: object) -> dict[str, object]:
        nonlocal render_calls
        if render_calls == 0:
            clock.now += 1.25
        render_calls += 1
        return real_renderer(**kwargs)

    monkeypatch.setattr(restore_drill, "render_restore_evidence", advancing_renderer)

    assert drill.run(_request()) == 0

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["measured"] is True
    assert report["rto_seconds"] == 3


class _CleanupObservingRunner(_FakeRunner):
    def __init__(self, output_path: Path, *, cleanup_status: int = 0) -> None:
        super().__init__(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
        self.output_path = output_path
        self.cleanup_status = cleanup_status
        self.evidence_present_during_cleanup = False

    def __call__(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if "rm" in command:
            self.evidence_present_during_cleanup = self.output_path.exists()
            if self.cleanup_status:
                return subprocess.CompletedProcess(command, self.cleanup_status, "", "")
        return super().__call__(command)


def test_success_evidence_is_persisted_before_cleanup_and_cleanup_failure_preserves_it(tmp_path: Path) -> None:
    output_path = tmp_path / "restore.json"
    fake = _CleanupObservingRunner(output_path, cleanup_status=2)
    secret_dir = _write_valid_secret_dir(tmp_path)
    drill = RestoreDrill(
        command_runner=fake,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=output_path,
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request()) == 2

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert fake.evidence_present_during_cleanup is True
    assert report["measured"] is True
    assert "cleanup_failed" in (tmp_path / "restore.log").read_text(encoding="utf-8")


def test_compose_up_failure_still_cleans_the_attempted_container(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        compose_up_status=2,
    )

    assert _drill(tmp_path, fake).run(_request()) == 2

    cleanup = [command for command in fake.commands if "rm" in command and "restore-db" in command]
    assert len(cleanup) == 1
    assert all(
        argument.startswith("bfx-dr-")
        for argument in cleanup[0]
        if argument.startswith("bfx-")
    )


class _HealthTimeoutRunner(_FakeRunner):
    def __init__(self) -> None:
        super().__init__(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
        self.health_timeouts: list[float | None] = []

    def __call__(self, command: tuple[str, ...], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if command[:3] == ("docker", "inspect", "--format={{.State.Health.Status}}"):
            self.health_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(command, timeout or 0)
        return super().__call__(command)


def test_health_inspect_timeout_uses_remaining_deadline_and_writes_failure(tmp_path: Path) -> None:
    fake = _HealthTimeoutRunner()

    assert _drill(tmp_path, fake).run(_request()) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "restore_command_failed"
    assert fake.health_timeouts and 0 < fake.health_timeouts[0] <= 600


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
        "DR_EGRESS_NETWORK_NAME",
        "DR_CONTAINER_NAME",
        "DATABASE_URL",
        "BFX_DEPLOYMENT_ENV",
        "DR_ACCOUNT_ID",
        "DR_PROJECTOR_VERSION",
        "DR_TARGET_BACKUP_LABEL",
        "DR_TARGET_TIME",
        "DR_PGBACKREST_SECRET_DIR",
    }
    cleanup = fake.commands[-4:]
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


def test_internal_egress_network_fails_before_disconnect_or_verifier(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        egress_internal="true\n",
    )

    assert _drill(tmp_path, fake).run(_request()) == 2

    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "network_not_internal"
    assert not any(
        command[:3] == ("docker", "network", "disconnect")
        or "verifier" in command
        for command in fake.commands
    )


def test_restore_disconnects_egress_and_proves_absence_before_verifier(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), "")
    )

    assert _drill(tmp_path, fake).run(_request()) == 0

    disconnect_index = next(
        index
        for index, command in enumerate(fake.commands)
        if command[:3] == ("docker", "network", "disconnect")
    )
    membership_index = next(
        index
        for index, command in enumerate(fake.commands)
        if command[:3]
        == ("docker", "inspect", "--format={{json .NetworkSettings.Networks}}")
    )
    verifier_index = next(
        index
        for index, command in enumerate(fake.commands)
        if "run" in command and "verifier" in command
    )
    assert disconnect_index < membership_index < verifier_index
    assert "--no-deps" in fake.commands[verifier_index]
    assert "DR_EGRESS_NETWORK_NAME=" in fake.env_text


def test_restore_rejects_egress_membership_before_verifier(tmp_path: Path) -> None:
    egress_name = "bfx-dr-20260904t031700z-a1b2c3d4e5f60718-egress"
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""),
        container_networks=json.dumps({egress_name: {}}),
    )

    assert _drill(tmp_path, fake).run(_request()) == 2

    assert not any("verifier" in command for command in fake.commands)
    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "restore_output_invalid"


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

    secret_dir = _write_valid_secret_dir(tmp_path)
    drill = RestoreDrill(
        command_runner=explode,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request()) == 2

    report = (tmp_path / "restore.json").read_text(encoding="utf-8")
    assert json.loads(report)["error_code"] == "restore_command_failed"
    assert "TOKEN-SENTINEL" not in report


def test_restore_rejects_invalid_secret_before_resource_creation(tmp_path: Path) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), "")
    )
    secret_dir = _write_valid_secret_dir(tmp_path)
    (secret_dir / "r2.conf").write_text(
        (secret_dir / "r2.conf").read_text(encoding="utf-8").replace(
            "opaque-access-key", "<ACCOUNT_ID>"
        ),
        encoding="utf-8",
    )
    drill = RestoreDrill(
        command_runner=fake,
        config_path=CONFIG_PATH,
        secret_dir=secret_dir,
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request()) == 2
    assert fake.commands == []
    report = (tmp_path / "restore.json").read_text(encoding="utf-8")
    assert json.loads(report)["error_code"] == "restore_output_invalid"
    assert "<ACCOUNT_ID>" not in report


def test_restore_secret_preflight_rejects_untracked_config_before_resource_creation(
    tmp_path: Path,
) -> None:
    fake = _FakeRunner(
        verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), "")
    )
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
    drill = RestoreDrill(
        command_runner=fake,
        config_path=config,
        secret_dir=_write_valid_secret_dir(tmp_path),
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request()) == 2
    assert fake.commands == []
    report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
    assert report["error_code"] == "restore_output_invalid"


@pytest.mark.parametrize("state", ("clean", "unstaged", "staged", "untracked", "wildcard", "symlink"))
def test_restore_secret_preflight_requires_exact_clean_tracked_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "pgbackrest.conf"
    if state == "symlink":
        target = tmp_path / "untracked-target.conf"
        target.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
        config.symlink_to(target)
    else:
        config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ("git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "user.name=Test", "-c", "user.email=test@invalid", *args),
            check=True, capture_output=True, text=True,
        )

    git("init", "-q")
    git("add", "pgbackrest.conf")
    git("commit", "-qm", "test fixture")
    if state in {"untracked", "wildcard"}:
        config = repo / ("untracked.conf" if state == "untracked" else "pgbackrest*.conf")
    if state != "clean":
        config.write_text("[global]\nrepo1-type=s3\n# changed\n", encoding="utf-8")
    if state == "staged":
        git("add", "pgbackrest.conf")
    monkeypatch.setattr(restore_drill, "ROOT", repo)
    fake = _FakeRunner(verifier=subprocess.CompletedProcess(("fake",), 0, _replay_report(), ""))
    drill = RestoreDrill(
        command_runner=fake,
        config_path=config,
        secret_dir=_write_valid_secret_dir(tmp_path),
        output_path=tmp_path / "restore.json",
        run_id_factory=lambda: "20260904T031700Z-a1b2c3d4e5f60718",
        postgres_uid=os.getuid(),
        postgres_gid=os.getgid(),
    )

    assert drill.run(_request()) == (0 if state == "clean" else 2)
    if state != "clean":
        assert fake.commands == []
        report = json.loads((tmp_path / "restore.json").read_text(encoding="utf-8"))
        assert report["error_code"] == "restore_output_invalid"
