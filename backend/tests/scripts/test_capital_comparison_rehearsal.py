"""Offline contract for the one-shot comparison on an isolated restore."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
DRILL_PATH = ROOT / "deploy/vm/pgbackrest/restore_drill.py"
CONFIG_PATH = ROOT / "deploy/vm/pgbackrest/pgbackrest.conf"
RUN_ID = "20260904T031700Z-a1b2c3d4e5f60718"
RESOURCE_ID = RUN_ID.lower()
ACCOUNT = "3f19d046-5030-494c-9a0a-9573bb890c1f"
IMAGE = "ghcr.io/example/bfx-backend@sha256:" + "b" * 64
REVISION = "a" * 40
PASSWORD = "DATABASE-PASSWORD-SENTINEL"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


restore_drill = _load_module("capital_comparison_restore_drill", DRILL_PATH)


class Runner:
    def __init__(
        self, *, comparison_exit: int = 0, timeout: bool = False,
        bootstrap_failure: bool = False, cleanup_failure: bool = False,
        deployed_image: str = IMAGE,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.sql: list[tuple[tuple[str, ...], str]] = []
        self.comparison_exit = comparison_exit
        self.timeout = timeout
        self.bootstrap_failure = bootstrap_failure
        self.cleanup_failure = cleanup_failure
        self.deployed_image = deployed_image
        self.private_paths: list[Path] = []
        self.private_modes: list[int] = []
        self.dsn = ""
        self.manifest: dict[str, object] = {}
        self.cells = ""
        self.env_path: Path | None = None
        self.env_mode: int | None = None
        self.env_text = ""

    def __call__(
        self, command: tuple[str, ...], *, timeout: float | None = None,
        input_text: str | None = None, env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if input_text is not None:
            self.sql.append((command, input_text))
        if command[:3] == ("docker", "inspect", "--format={{.Config.Image}}"):
            return subprocess.CompletedProcess(command, 0, self.deployed_image + "\n", "")
        if command[:3] == ("docker", "image", "inspect"):
            return subprocess.CompletedProcess(command, 0, REVISION + "\n", "")
        if command[:3] == ("docker", "network", "inspect"):
            value = "false" if command[-1].endswith("-egress") else "true"
            return subprocess.CompletedProcess(command, 0, value + "\n", "")
        if command[:3] == ("docker", "inspect", "--format={{json .NetworkSettings.Networks}}"):
            networks = {f"bfx-dr-{RESOURCE_ID}-net": {}}
            return subprocess.CompletedProcess(command, 0, json.dumps(networks), "")
        if command[:3] == ("docker", "inspect", "--format={{.State.Health.Status}}"):
            return subprocess.CompletedProcess(command, 0, "healthy\n", "")
        if input_text == "SELECT pg_is_in_recovery();\n":
            return subprocess.CompletedProcess(command, 0, "f\n", "")
        if input_text is not None and "CREATE ROLE" in input_text:
            return subprocess.CompletedProcess(command, 2 if self.bootstrap_failure else 0, "", PASSWORD)
        if "--env-file" in command:
            self.env_path = Path(command[command.index("--env-file") + 1])
            self.env_mode = self.env_path.stat().st_mode & 0o777
            self.env_text = self.env_path.read_text(encoding="utf-8")
        if "bfx_funding_bot.apps.capital_comparison" in command:
            volumes = [command[index + 1] for index, arg in enumerate(command) if arg == "--volume"]
            self.private_paths = [Path(item.split(":", 1)[0]) for item in volumes]
            self.private_modes = [path.stat().st_mode & 0o777 for path in self.private_paths]
            self.dsn = self.private_paths[0].read_text(encoding="utf-8")
            self.manifest = json.loads(self.private_paths[1].read_text(encoding="utf-8"))
            self.cells = self.private_paths[2].read_text(encoding="utf-8")
            if self.timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            records = (
                {"status": "equal", "scope": {"account_id": ACCOUNT}},
                {"kind": "summary", "exit_code": self.comparison_exit},
            )
            return subprocess.CompletedProcess(
                command, self.comparison_exit,
                "".join(json.dumps(record) + "\n" for record in records), PASSWORD,
            )
        if self.cleanup_failure and command[:3] == ("docker", "network", "rm"):
            return subprocess.CompletedProcess(command, 2, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: Runner,
) -> tuple[object, object, Path]:
    monkeypatch.setattr(restore_drill, "_config_is_clean_tracked", lambda _: True)
    monkeypatch.setattr(restore_drill, "validate_secret_dir", lambda *_args, **_kwargs: None)
    cells = tmp_path / "cells.live.yaml"
    cells.write_text("cells: []\n", encoding="utf-8")
    evidence_root = tmp_path / "evidence"
    drill = restore_drill.RestoreDrill(
        command_runner=runner, config_path=CONFIG_PATH, secret_dir=tmp_path / "no-secrets-read",
        rehearsal_evidence_root=evidence_root, run_id_factory=lambda: RUN_ID,
        password_factory=lambda: PASSWORD,
    )
    request = restore_drill.RehearsalRequest(
        backup_label="20260904031700-F", target_time="2026-09-04T04:00:00Z",
        database_name="bfx", image=IMAGE, cells_path=cells,
        scopes=(f"{ACCOUNT}:prod", f"{ACCOUNT}:shadow"),
    )
    return drill, request, evidence_root / RESOURCE_ID


def _comparison_command(runner: Runner) -> tuple[str, ...]:
    return next(command for command in runner.commands
                if "bfx_funding_bot.apps.capital_comparison" in command)


def _assert_cleaned(runner: Runner) -> None:
    assert runner.env_path is not None and not runner.env_path.exists()
    assert runner.private_paths and all(not path.exists() for path in runner.private_paths)
    assert not runner.private_paths[0].parent.exists()
    assert any(command[:4] == ("docker", "container", "rm", "--force") for command in runner.commands)
    assert any(command[:3] == ("docker", "volume", "rm") for command in runner.commands)
    assert sum(command[:3] == ("docker", "network", "rm") for command in runner.commands) == 2


def test_rehearsal_uses_only_restored_copy_and_hardened_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    runner = Runner()
    drill, request, evidence_dir = _setup(tmp_path, monkeypatch, runner)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("production read in rehearsal")

    monkeypatch.setattr(drill, "_latest_backup_label", forbidden)
    monkeypatch.setattr(drill, "_production_prefix", forbidden)
    assert drill.run_rehearsal(request) == 0

    command = _comparison_command(runner)
    assert runner.manifest == {
        "mode": "rehearsal", "host": f"bfx-dr-{RESOURCE_ID}-db", "port": 5432,
        "database": "bfx", "user": f"bfx_dr_{RESOURCE_ID.replace('-', '_')}",
        "run_id": RESOURCE_ID, "now_ms": 1788494400000,
    }
    assert runner.dsn.startswith("postgresql+asyncpg://bfx_dr_")
    assert PASSWORD in runner.dsn
    assert runner.cells == "cells: []\n"
    assert runner.private_modes == [0o600] * 3
    assert runner.env_mode == 0o600
    assert "DR_ACCOUNT_ID=\n" in runner.env_text
    assert "DR_PROJECTOR_VERSION=\n" in runner.env_text
    assert command[command.index("--network") + 1] == f"bfx-dr-{RESOURCE_ID}-net"
    for flag in ("--rm", "--read-only", "--tmpfs", "--cap-drop", "--security-opt", "--pids-limit"):
        assert flag in command
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert command[command.index("--entrypoint") + 1] == "python"
    assert command[command.index("--code-revision") + 1] == REVISION
    assert command.count("--scope") == 2
    assert all(item.endswith(":ro") for item in (
        command[index + 1] for index, arg in enumerate(command) if arg == "--volume"
    ))
    assert "--env-file" not in command
    assert "--env" not in command
    assert "/var/run/docker.sock" not in " ".join(command)
    assert PASSWORD not in " ".join(map(str, runner.commands))
    assert PASSWORD not in capsys.readouterr().out
    assert PASSWORD not in (evidence_dir / "comparison.jsonl").read_text(encoding="utf-8")
    assert (evidence_dir / "summary.json").read_text(encoding="utf-8").strip() == \
        '{"exit_code":0,"kind":"summary"}'
    result = json.loads((evidence_dir / "result.json").read_text())
    assert result["exit_code"] == 0
    assert result["cells_sha256"] == hashlib.sha256(b"cells: []\n").hexdigest()
    assert (evidence_dir / "comparison.jsonl").stat().st_mode & 0o777 == 0o600
    assert (evidence_dir / "result.json").stat().st_mode & 0o777 == 0o600

    comparison_index = runner.commands.index(command)
    before = runner.commands[:comparison_index]
    assert any(item[:3] == ("docker", "network", "disconnect") for item in before)
    assert any(item[:3] == ("docker", "inspect", "--format={{json .NetworkSettings.Networks}}")
               for item in before)
    grant_command, grant_sql = next(item for item in runner.sql if "CREATE ROLE" in item[1])
    assert grant_command[5] == f"bfx-dr-{RESOURCE_ID}-db"
    assert grant_command in before
    # Production shape: a LOGIN member of the reader group (the tool does SET LOCAL ROLE),
    # not table-level legacy grants.
    assert re.search(r'GRANT bfx_cutover_reader TO "bfx_dr_[a-z0-9_]+";', grant_sql)
    assert "GRANT SELECT" not in grant_sql
    assert 'public."' not in grant_sql
    assert all("pgbackrest" not in command and "bfx-postgres" not in command
               for command in runner.commands)
    _assert_cleaned(runner)


@pytest.mark.parametrize("comparison_exit", [0, 1, 3])
def test_rehearsal_propagates_comparison_exit_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comparison_exit: int,
) -> None:
    runner = Runner(comparison_exit=comparison_exit)
    drill, request, evidence_dir = _setup(tmp_path, monkeypatch, runner)
    assert drill.run_rehearsal(request) == comparison_exit
    assert json.loads((evidence_dir / "summary.json").read_text())["exit_code"] == comparison_exit
    assert json.loads((evidence_dir / "result.json").read_text())["exit_code"] == comparison_exit
    _assert_cleaned(runner)


@pytest.mark.parametrize(("case", "expected_error"), [
    ("timeout", "restore_command_failed"),
    ("cleanup", "cleanup_failed"),
])
def test_rehearsal_failure_cleans_and_returns_operational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected_error: str,
) -> None:
    runner = Runner(timeout=case == "timeout", cleanup_failure=case == "cleanup")
    drill, request, evidence_dir = _setup(tmp_path, monkeypatch, runner)
    assert drill.run_rehearsal(request) == 3
    result = json.loads((evidence_dir / "result.json").read_text())
    assert result["error_code"] == expected_error
    assert result["exit_code"] == 3
    _assert_cleaned(runner)


def test_rehearsal_image_mismatch_creates_no_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = Runner(deployed_image="ghcr.io/example/other@sha256:" + "c" * 64)
    drill, request, evidence_dir = _setup(tmp_path, monkeypatch, runner)
    assert drill.run_rehearsal(request) == 3
    assert json.loads((evidence_dir / "result.json").read_text())["error_code"] == \
        "rehearsal_image_mismatch"
    assert not any(command[:3] in {("docker", "network", "create"),
                                   ("docker", "volume", "create")} for command in runner.commands)
    assert runner.env_path is None
    assert not any(path.name.startswith("bfx-rehearsal-") for path in tmp_path.iterdir())


def test_rehearsal_rejects_secret_shaped_image_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    runner = Runner()
    drill, request, evidence_dir = _setup(tmp_path, monkeypatch, runner)
    secret_image = f"postgresql+asyncpg://role:{PASSWORD}@production/bfx"
    assert drill.run_rehearsal(replace(request, image=secret_image)) == 3
    assert runner.commands == []
    assert not evidence_dir.exists()
    assert PASSWORD not in capsys.readouterr().out


def test_rehearsal_bootstrap_failure_cleans_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = Runner(bootstrap_failure=True)
    drill, request, evidence_dir = _setup(tmp_path, monkeypatch, runner)
    private_paths: list[Path] = []
    original = restore_drill._write_private_file

    def capture(path: Path, contents: bytes) -> None:
        private_paths.append(path)
        original(path, contents)

    monkeypatch.setattr(restore_drill, "_write_private_file", capture)
    assert drill.run_rehearsal(request) == 3
    assert private_paths and all(not path.exists() for path in private_paths)
    assert not private_paths[0].parent.exists()
    assert runner.env_path is not None and not runner.env_path.exists()
    assert json.loads((evidence_dir / "result.json").read_text())["error_code"] == \
        "restore_command_failed"
    assert not any("bfx_funding_bot.apps.capital_comparison" in command
                   for command in runner.commands)
