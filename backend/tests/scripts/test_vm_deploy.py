"""bfx-deploy decision flow against a simulated VM (no Docker, no network).

The simulated host answers the exact argv bfx-deploy issues (git through
runuser, docker pull/run/compose/inspect/exec/stop, backup.sh) and keeps just
enough state -- schema revision, running images, health by digest -- to prove
the ordering and fail-safe rules: backup before migration, no migration after a
failed backup, rollback only when no migration ran, bot stopped otherwise.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx = _load("vm_ops_bfx_deploy_under_test", ROOT / "deploy/vm/ops/bfx_deploy.py")

BACKEND = "ghcr.io/will413028/bfx-funding-bot-backend"
FRONTEND = "ghcr.io/will413028/bfx-funding-bot-frontend"
REV_OLD = "a" * 40
REV_NEW = "b" * 40
OLD_B, OLD_F = "sha256:" + "1" * 64, "sha256:" + "2" * 64
NEW_B, NEW_F = "sha256:" + "3" * 64, "sha256:" + "4" * 64
RULES_TEXT = (ROOT / "deploy/change-class.yaml").read_text(encoding="utf-8")
COMPOSE_TEXT = (ROOT / "deploy/vm/docker-compose.app.yml").read_text(encoding="utf-8")
LIVE_ENV_TEXT = (ROOT / "deploy/vm/live.env").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- fakes


class FakeRegistry:
    def __init__(self) -> None:
        self.tags = {
            (BACKEND, "main"): NEW_B, (BACKEND, f"sha-{REV_NEW}"): NEW_B,
            (FRONTEND, f"sha-{REV_NEW}"): NEW_F,
            (BACKEND, f"sha-{REV_OLD}"): OLD_B, (FRONTEND, f"sha-{REV_OLD}"): OLD_F,
        }
        self.labels = {(BACKEND, NEW_B): REV_NEW, (FRONTEND, NEW_F): REV_NEW,
                       (BACKEND, OLD_B): REV_OLD, (FRONTEND, OLD_F): REV_OLD}

    def resolve(self, repository: str, tag: str) -> str:
        if (repository, tag) not in self.tags:
            raise bfx.DiscoveryError(f"registry_not_found:{tag}")
        return self.tags[(repository, tag)]

    def revision(self, repository: str, digest: str) -> str:
        return self.labels[(repository, digest)]


class FakeLedger:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.rows: list[Any] = []
        self.fail_append = False

    def seed(self, revision: str, backend: str, frontend: str, outcome: str,
             klass: str = "standard") -> None:
        self.rows.append(bfx.LedgerEntry(
            started_at="2026-09-24T00:00:00+00:00", finished_at="2026-09-24T00:01:00+00:00",
            source_revision=revision, backend_digest=backend, frontend_digest=frontend,
            change_class=klass, migrations_applied=False, outcome=outcome, detail="seed"))

    def _row(self, index: int) -> Any:
        entry = self.rows[index]
        return bfx.LedgerRow(id=index + 1, source_revision=entry.source_revision,
                             backend_digest=entry.backend_digest,
                             frontend_digest=entry.frontend_digest,
                             change_class=entry.change_class, outcome=entry.outcome,
                             migrations_applied=entry.migrations_applied)

    def read(self) -> Any:
        if not self.exists:
            return bfx.LedgerView(exists=False, last_attempt=None, last_success=None)
        successes = [i for i, row in enumerate(self.rows) if row.outcome == "deployed"]
        return bfx.LedgerView(
            exists=True,
            last_attempt=self._row(len(self.rows) - 1) if self.rows else None,
            last_success=self._row(successes[-1]) if successes else None,
        )

    def append(self, entry: Any) -> int:
        if self.fail_append:
            raise bfx.CommandError("ledger_psql_exit_3")
        self.rows.append(entry)
        return len(self.rows)

    @property
    def last(self) -> Any:
        return self.rows[-1]


def inspect_record(service: str, image: str, digest: str, revision: str, klass: str) -> dict[str, Any]:
    backend = service != "frontend"
    env = [f"BFX_IMAGE_DIGEST={digest}", f"BFX_SOURCE_REVISION={revision}",
           f"BFX_CHANGE_CLASS={klass}", "PYTHONPATH=", "LD_PRELOAD=", "PATH=/usr/bin"]
    if backend:
        env.append("PYTHONDONTWRITEBYTECODE=1")
    return {
        "Name": "/" + bfx.CONTAINERS[service],
        "Config": {
            "Image": image, "User": "1000:1000" if backend else "nextjs", "WorkingDir": "/app",
            "Cmd": list(bfx.BACKEND_COMMANDS[service] if backend else bfx.FRONTEND_COMMAND),
            "Entrypoint": None, "Env": env,
        },
        "HostConfig": {
            "ReadonlyRootfs": True, "Privileged": False, "CapAdd": None, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {} if backend else {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3001"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        },
        "NetworkSettings": {"Networks": {"bfx_default": {
            "Aliases": ["bfx-bot", "bot"] if service == "bot" else [service],
            "DNSNames": [bfx.CONTAINERS[service]],
        }}},
        "Mounts": [],
    }


@dataclass
class FakeHost:
    """Answers bfx-deploy's argv like the VM would; records every call."""

    mirror: Path
    calls: list[tuple[str, ...]] = field(default_factory=list)
    envs: list[Mapping[str, str] | None] = field(default_factory=list)
    commits: set[str] = field(default_factory=lambda: {REV_OLD, REV_NEW})
    on_main: set[str] = field(default_factory=lambda: {REV_OLD, REV_NEW})
    files: dict[tuple[str, str], str] = field(default_factory=dict)
    diffs: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    heads: tuple[str, ...] = ("h2",)
    current: tuple[str, ...] = ("h2",)
    upgrade_result: str = "ok"  # ok | fail_unchanged | fail_partial
    backup_ok: bool = True
    compose_fail: set[str] = field(default_factory=set)
    unhealthy: set[str] = field(default_factory=set)
    tampered: dict[str, Callable[[dict[str, Any]], None]] = field(default_factory=dict)
    foreign: list[str] = field(default_factory=list)
    running: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    stopped: list[str] = field(default_factory=list)
    docker_configs: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for revision in (REV_OLD, REV_NEW):
            self.files[(revision, bfx.COMPOSE_PATH)] = COMPOSE_TEXT
            self.files[(revision, bfx.LIVE_ENV_PATH)] = LIVE_ENV_TEXT
            self.files[(revision, "deploy/change-class.yaml")] = RULES_TEXT
        self.diffs.setdefault((REV_OLD, REV_NEW), ["docs/runbooks/offsite-dr.md"])

    def index(self, predicate: Callable[[tuple[str, ...]], bool]) -> int:
        return next(i for i, call in enumerate(self.calls) if predicate(call))

    def count(self, predicate: Callable[[tuple[str, ...]], bool]) -> int:
        return sum(1 for call in self.calls if predicate(call))

    def __call__(self, argv: Sequence[str], *, timeout: float, input_text: str | None = None,
                 env: Mapping[str, str] | None = None) -> Any:
        call = tuple(argv)
        self.calls.append(call)
        self.envs.append(env)
        ok = bfx.CommandResult(0, "", "")
        if call[:4] == ("runuser", "-u", "ubuntu", "--") and call[4] == "git":
            assert call[5:7] == ("-C", str(self.mirror))
            return self._git(call[7:])
        if call[:4] == ("runuser", "-u", "ubuntu", "--") and call[4].endswith("backup.sh"):
            assert call[5:] == ("--type", "diff")
            return ok if self.backup_ok else bfx.CommandResult(2, "", "backup_evidence_unavailable")
        if call[:2] == ("docker", "ps"):
            lines = [f"{name}\t" for name in self.foreign]
            lines += [f"{bfx.CONTAINERS[s]}\tbfx-app" for s in self.running]
            return bfx.CommandResult(0, "\n".join(lines), "")
        if call[:2] == ("docker", "pull"):
            if env and "DOCKER_CONFIG" in env:
                config = Path(env["DOCKER_CONFIG"]) / "config.json"
                self.docker_configs.append(json.loads(config.read_text()))
            return ok
        if call[:2] == ("docker", "run"):
            return self._alembic(call)
        if call[:2] == ("docker", "compose"):
            return self._compose(call, env)
        if call[:2] == ("docker", "inspect"):
            return self._inspect(call)
        if call[:2] == ("docker", "exec"):
            container = call[2]
            service = next(s for s, c in bfx.CONTAINERS.items() if c == container)
            healthy = service in self.running and self.running[service][1] not in self.unhealthy
            return bfx.CommandResult(0 if healthy else 1, "", "")
        if call[:2] == ("docker", "stop"):
            self.stopped.append(call[-1])
            return ok
        if call[:2] in {("docker", "tag"), ("docker", "rm")} or call[:3] in {
            ("docker", "image", "ls"), ("docker", "image", "rm")
        }:
            return ok
        raise AssertionError(f"unexpected command: {call}")

    def frontend_status(self, url: str, timeout: float) -> int:
        assert url == "http://127.0.0.1:3001/"
        running = self.running.get("frontend")
        return 200 if running and running[1] not in self.unhealthy else 503

    def _git(self, args: tuple[str, ...]) -> Any:
        if args[0] == "fetch":
            assert args[-1] == "+refs/heads/main:refs/remotes/origin/main"
            return bfx.CommandResult(0, "", "")
        if args[:2] == ("cat-file", "-e"):
            return bfx.CommandResult(0 if args[2].removesuffix("^{commit}") in self.commits else 128, "", "")
        if args[:2] == ("merge-base", "--is-ancestor"):
            return bfx.CommandResult(0 if args[2] in self.on_main else 1, "", "")
        if args[0] == "show":
            revision, _, path = args[1].partition(":")
            content = self.files.get((revision, path))
            return bfx.CommandResult(0, content, "") if content is not None else bfx.CommandResult(128, "", "fatal")
        if args[:4] == ("diff", "--name-only", "--no-renames", "-z"):
            paths = self.diffs.get((args[4], args[5]))
            if paths is None:
                return bfx.CommandResult(128, "", "fatal")
            return bfx.CommandResult(0, "".join(p + "\0" for p in paths), "")
        raise AssertionError(f"unexpected git: {args}")

    def _alembic(self, call: tuple[str, ...]) -> Any:
        at = call.index("/app/.venv/bin/alembic")
        assert "--read-only" in call and "--cap-drop=ALL" in call and "--pull=never" in call
        args = call[at + 1:]
        if args == ("heads",):
            return bfx.CommandResult(0, "".join(f"{h} (head)\n" for h in self.heads), "")
        if args == ("current",):
            return bfx.CommandResult(0, "".join(f"{c}\n" for c in self.current), "")
        if args == ("upgrade", "head"):
            if self.upgrade_result == "ok":
                self.current = self.heads
                return bfx.CommandResult(0, "", "")
            if self.upgrade_result == "fail_partial":
                self.current = ("h1b",)
            return bfx.CommandResult(1, "", "boom")
        raise AssertionError(f"unexpected alembic: {args}")

    def _compose(self, call: tuple[str, ...], env: Mapping[str, str] | None) -> Any:
        assert call[:5] == ("docker", "compose", "-p", "bfx-app", "-f")
        compose_file = Path(call[5])
        assert compose_file.read_text() == COMPOSE_TEXT
        assert (compose_file.parent / "live.env").read_text() == LIVE_ENV_TEXT
        assert call[6:] == ("up", "--detach", "--no-deps", "bot", "webapi", "frontend")
        assert env is not None
        if env["BFX_BACKEND_DIGEST"] in self.compose_fail:
            return bfx.CommandResult(1, "", "compose failed")
        revision, klass = env["BFX_SOURCE_REVISION"], env["BFX_CHANGE_CLASS"]
        for service in ("bot", "webapi"):
            self.running[service] = (env["BFX_BACKEND_IMAGE"], env["BFX_BACKEND_DIGEST"], revision, klass)
        self.running["frontend"] = (env["BFX_FRONTEND_IMAGE"], env["BFX_FRONTEND_DIGEST"], revision, klass)
        return bfx.CommandResult(0, "", "")

    def _inspect(self, call: tuple[str, ...]) -> Any:
        if "--format" in call:
            lines = [f"/{bfx.CONTAINERS[s]} 0 true" for s in bfx.SERVICES if s in self.running]
            return bfx.CommandResult(0, "\n".join(lines), "")
        records = []
        for service in bfx.SERVICES:
            if service not in self.running:
                return bfx.CommandResult(1, "[]", "no such container")
            image, digest, revision, klass = self.running[service]
            record = inspect_record(service, image, digest, revision, klass)
            if digest in self.tampered:
                self.tampered[digest](record)
            records.append(record)
        return bfx.CommandResult(0, json.dumps(records), "")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Harness:
    host: FakeHost
    registry: FakeRegistry
    ledger: FakeLedger
    clock: FakeClock
    notices: list[tuple[str, str]]
    settings: Any

    def deployer(self, **overrides: Any) -> Any:
        settings = self.settings
        if overrides:
            fields = {name: getattr(settings, name) for name in settings.__dataclass_fields__}
            settings = bfx.Settings(**{**fields, **overrides})
        return bfx.Deployer(
            settings, runner=self.host, registry=self.registry, ledger=self.ledger,
            notify=lambda level, text: self.notices.append((level, text)),
            clock=self.clock.time, sleep=self.clock.sleep, http=self.host.frontend_status,
            secret_check=lambda path: None,
        )

    def run(self, **overrides: Any) -> int:
        return int(self.deployer(**overrides).run())


VALID_ENV = {
    "bot.env": "DATABASE_URL=postgresql://bfx_bot:x@bfx-postgres:5432/bfx\nBFX_ADMIN_TOKEN=t\n",
    "webapi.env": "DATABASE_URL=postgresql://bfx_webapi:x@bfx-postgres:5432/bfx\n",
    "frontend.env": "DATABASE_URL=postgresql://bfx_webauth:x@bfx-postgres:5432/bfx\n",
    "migrate.env": "DATABASE_URL=postgresql://bfx:x@bfx-postgres:5432/bfx\n",
}


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name, text in VALID_ENV.items():
        (runtime / name).write_text(text)
    mirror = tmp_path / "mirror"
    settings = bfx.Settings(
        mirror=mirror, mirror_user="ubuntu", runtime_dir=runtime,
        state_dir=tmp_path / "state", lock_file=tmp_path / "deploy.lock",
        pgbackrest_dir=tmp_path / "pgbackrest", backup_user="ubuntu",
    )
    ledger = FakeLedger()
    ledger.seed(REV_OLD, OLD_B, OLD_F, "deployed")
    return Harness(host=FakeHost(mirror=mirror), registry=FakeRegistry(), ledger=ledger,
                   clock=FakeClock(), notices=[], settings=settings)


def _is(prefix: tuple[str, ...]) -> Callable[[tuple[str, ...]], bool]:
    return lambda call: call[:len(prefix)] == prefix


def _alembic(*args: str) -> Callable[[tuple[str, ...]], bool]:
    return lambda call: call[:2] == ("docker", "run") and call[-len(args):] == args


BACKUP = lambda call: len(call) > 4 and call[4].endswith("backup.sh")  # noqa: E731
COMPOSE_UP = _is(("docker", "compose"))
STOP_BOT = _is(("docker", "stop"))
PULL = _is(("docker", "pull"))


# --------------------------------------------------------------------------- no-op


def test_nothing_happens_when_main_already_runs(harness: Harness) -> None:
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, "deployed")
    rows = len(harness.ledger.rows)
    assert harness.run() == 0
    assert harness.host.calls == []
    assert (harness.notices, len(harness.ledger.rows)) == ([], rows)


def test_a_digest_that_already_failed_is_not_retried_every_tick(harness: Harness) -> None:
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, "rolled_back")
    assert harness.run() == 0
    assert harness.host.calls == [] and harness.notices == []
    assert harness.run(retry=True) == 0
    assert harness.ledger.last.outcome == "deployed"


def test_second_run_is_skipped_while_the_lock_is_held(harness: Harness) -> None:
    harness.settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with harness.settings.lock_file.open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        assert harness.run() == 0
    assert harness.host.calls == []


# --------------------------------------------------------------------------- success


def test_standard_release_without_migration_deploys_by_digest(harness: Harness) -> None:
    assert harness.run() == 0
    host = harness.host
    assert host.count(BACKUP) == 0
    assert host.count(_alembic("upgrade", "head")) == 0
    assert host.index(PULL) < host.index(_alembic("current")) < host.index(COMPOSE_UP)
    env = host.envs[host.index(COMPOSE_UP)]
    assert env is not None
    assert env["BFX_BACKEND_IMAGE"] == f"{BACKEND}@{NEW_B}"
    assert env["BFX_FRONTEND_IMAGE"] == f"{FRONTEND}@{NEW_F}"
    assert (env["BFX_SOURCE_REVISION"], env["BFX_CHANGE_CLASS"]) == (REV_NEW, "standard")
    row = harness.ledger.last
    assert (row.outcome, row.change_class, row.migrations_applied) == ("deployed", "standard", False)
    assert (row.backend_digest, row.frontend_digest, row.source_revision) == (NEW_B, NEW_F, REV_NEW)
    assert harness.notices[-1][0] == "info"
    assert host.stopped == []
    assert host.count(_is(("docker", "tag", f"{BACKEND}@{NEW_B}", "bfx-bot:local"))) == 1


def test_pending_migration_is_preceded_by_a_successful_backup(harness: Harness) -> None:
    harness.host.current = ("h1",)
    assert harness.run() == 0
    host = harness.host
    assert host.index(BACKUP) < host.index(_alembic("upgrade", "head")) < host.index(COMPOSE_UP)
    assert harness.ledger.last.migrations_applied is True
    assert harness.ledger.last.outcome == "deployed"


def test_material_path_in_the_diff_marks_the_deployment_material(harness: Harness) -> None:
    harness.host.diffs[(REV_OLD, REV_NEW)] = ["docs/a.md", "backend/src/bfx_funding_bot/main.py"]
    assert harness.run() == 0
    env = harness.host.envs[harness.host.index(COMPOSE_UP)]
    assert env is not None and env["BFX_CHANGE_CLASS"] == "material"
    assert harness.ledger.last.change_class == "material"
    assert "backend/src/bfx_funding_bot/main.py" in harness.ledger.last.detail


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda h: h.host.commits.discard(REV_OLD), "previous_revision_unknown"),
    (lambda h: h.host.files.pop((REV_NEW, "deploy/change-class.yaml")), "rules_unavailable"),
    (lambda h: h.host.files.__setitem__((REV_NEW, "deploy/change-class.yaml"), "version: 9\n"), "rules_invalid"),
    (lambda h: h.host.diffs.pop((REV_OLD, REV_NEW)), "diff_failed"),
])
def test_undecidable_class_is_material(harness: Harness, mutate: Callable[[Harness], None],
                                       reason: str) -> None:
    mutate(harness)
    assert harness.run() == 0
    assert harness.ledger.last.change_class == "material"
    assert reason in harness.ledger.last.detail


def test_first_deployment_is_material(harness: Harness) -> None:
    harness.ledger.rows.clear()
    harness.ledger.exists = False
    assert harness.run() == 0
    assert harness.ledger.last.change_class == "material"
    assert "no_previous_deployment" in harness.ledger.last.detail


def test_operator_can_raise_but_the_tool_never_lowers(harness: Harness) -> None:
    assert harness.run(force_material=True) == 0
    assert harness.ledger.last.change_class == "material"
    assert "operator_forced" in harness.ledger.last.detail


def test_pull_uses_a_throwaway_docker_config_when_credentials_exist(harness: Harness) -> None:
    deployer = harness.deployer()
    deployer._registry_credentials = ("will413028", "ghp_token")
    assert deployer.run() == 0
    assert len(harness.host.docker_configs) == 2
    assert set(harness.host.docker_configs[0]["auths"]) == {"ghcr.io"}
    leftovers = list((harness.settings.state_dir).glob("docker-auth-*"))
    assert leftovers == []


# --------------------------------------------------------------------------- refusals


def test_backup_failure_never_migrates_and_leaves_the_running_release(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.backup_ok = False
    assert harness.run() == 1
    host = harness.host
    assert host.count(_alembic("upgrade", "head")) == 0
    assert host.count(COMPOSE_UP) == 0 and host.stopped == []
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", False)
    assert "backup_failed" in row.detail
    assert harness.notices[-1][0] == "critical"


def test_revision_not_on_main_is_refused_before_pulling(harness: Harness) -> None:
    harness.host.on_main.discard(REV_NEW)
    assert harness.run() == 1
    assert harness.host.count(PULL) == 0
    assert "precheck:revision_not_on_main" in harness.ledger.last.detail


def test_old_containers_holding_the_names_block_the_deploy(harness: Harness) -> None:
    harness.host.foreign = ["bfx-bot"]
    assert harness.run() == 1
    assert harness.host.count(PULL) == 0 and harness.host.count(COMPOSE_UP) == 0
    assert "foreign_container_holds_name:bfx-bot" in harness.ledger.last.detail


@pytest.mark.parametrize(("file", "text", "code"), [
    ("bot.env", "PYTHONPATH=/tmp/x\n", "env_file_forbidden_key:bot.env:PYTHONPATH"),
    ("frontend.env", "NODE_OPTIONS=--require /x\n", "env_file_forbidden_key:frontend.env:NODE_OPTIONS"),
    ("webapi.env", "BFX_CHANGE_CLASS=standard\n", "env_file_forbidden_key:webapi.env:BFX_CHANGE_CLASS"),
    ("bot.env", "SECRET=abc$def\n", "env_file_value_ambiguous_for_compose:bot.env:SECRET"),
    ("bot.env", 'SECRET="quoted"\n', "env_file_value_ambiguous_for_compose:bot.env:SECRET"),
    ("bot.env", "SECRET=value #comment\n", "env_file_value_ambiguous_for_compose:bot.env:SECRET"),
    ("bot.env", "lowercase=x\n", "env_file_invalid_line:bot.env"),
])
def test_env_files_that_compose_could_misread_block_before_any_change(
    harness: Harness, file: str, text: str, code: str,
) -> None:
    path = harness.settings.runtime_dir / file
    path.write_text(path.read_text() + text)
    assert harness.run() == 1
    assert harness.host.count(PULL) == 0
    assert f"precheck:{code}" in harness.ledger.last.detail
    assert "abc" not in harness.ledger.last.detail and "quoted" not in harness.ledger.last.detail


def test_migrate_env_keeps_docker_env_file_literal_semantics() -> None:
    # One-shots use `docker run --env-file`, which never interpolates or unquotes.
    assert bfx.parse_env_file("DATABASE_URL=postgresql://u:p$w@h/db\n", name="migrate.env",
                              compose=False) == {"DATABASE_URL": "postgresql://u:p$w@h/db"}


def test_repository_live_env_is_safe_for_compose() -> None:
    values = bfx.parse_env_file(LIVE_ENV_TEXT, name="live.env", compose=True)
    assert values["BFX_DEPLOYMENT_ENV"] == "prod"


def test_migration_failure_with_unchanged_schema_keeps_the_release_running(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.upgrade_result = "fail_unchanged"
    assert harness.run() == 1
    assert harness.host.count(COMPOSE_UP) == 0 and harness.host.stopped == []
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", False)
    assert "schema unchanged" in row.detail


def test_partial_migration_stops_the_bot_and_never_rolls_back(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.upgrade_result = "fail_partial"
    assert harness.run() == 1
    assert harness.host.count(COMPOSE_UP) == 0
    assert harness.host.stopped == ["bfx-bot"]
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", True)
    assert harness.notices[-1][0] == "critical"


# --------------------------------------------------------------------------- health


def test_unhealthy_release_without_migration_rolls_back_to_previous_digests(harness: Harness) -> None:
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    host = harness.host
    ups = [i for i, call in enumerate(host.calls) if COMPOSE_UP(call)]
    assert len(ups) == 2
    rollback_env = host.envs[ups[1]]
    assert rollback_env is not None
    assert rollback_env["BFX_BACKEND_IMAGE"] == f"{BACKEND}@{OLD_B}"
    assert rollback_env["BFX_FRONTEND_IMAGE"] == f"{FRONTEND}@{OLD_F}"
    assert rollback_env["BFX_SOURCE_REVISION"] == REV_OLD
    assert Path(host.calls[ups[1]][5]).parent.name == REV_OLD
    assert host.running["bot"][1] == OLD_B and host.stopped == []
    row = harness.ledger.last
    assert (row.outcome, row.backend_digest) == ("rolled_back", NEW_B)
    assert f"rolled_back_to={REV_OLD}" in row.detail
    assert harness.notices[-1][0] == "warning"
    assert host.count(_is(("docker", "tag"))) == 0


def test_unhealthy_release_after_migration_stops_the_bot_without_rollback(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    host = harness.host
    assert host.count(COMPOSE_UP) == 1
    assert host.stopped == ["bfx-bot"]
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", True)
    assert "no automatic rollback" in row.detail
    assert harness.notices[-1][0] == "critical"


def test_unhealthy_first_deployment_stops_the_bot(harness: Harness) -> None:
    harness.ledger.rows.clear()
    harness.host.unhealthy = {NEW_F}
    assert harness.run() == 1
    assert harness.host.count(COMPOSE_UP) == 1
    assert harness.host.stopped == ["bfx-bot"]
    assert "no previous deployment" in harness.ledger.last.detail


def test_rollback_that_also_fails_stops_the_bot(harness: Harness) -> None:
    harness.host.unhealthy = {NEW_B, OLD_B}
    assert harness.run() == 1
    assert harness.host.stopped == ["bfx-bot"]
    row = harness.ledger.last
    assert row.outcome == "failed" and "rollback_failed" in row.detail


def test_container_missing_hardening_is_treated_as_a_failed_start(harness: Harness) -> None:
    harness.host.tampered[NEW_B] = lambda record: record["HostConfig"].update(ReadonlyRootfs=False)
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "rolled_back"
    assert "container_hardening_mismatch:bot:read_only,webapi:read_only" in row.detail


def test_compose_failure_rolls_back(harness: Harness) -> None:
    harness.host.compose_fail = {NEW_B}
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "rolled_back" and "compose_up_failed" in row.detail
    assert harness.host.running["bot"][1] == OLD_B


def test_compose_failure_on_rollback_too_stops_the_bot(harness: Harness) -> None:
    harness.host.compose_fail = {NEW_B, OLD_B}
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "failed" and "rollback_failed:compose_up_failed" in row.detail
    assert harness.host.stopped == ["bfx-bot"]


def test_health_timeout_waits_the_configured_budget_then_rolls_back(harness: Harness) -> None:
    harness.host.unhealthy = {NEW_F}
    start = harness.clock.now
    assert harness.run() == 1
    assert "health_timeout:frontend" in harness.ledger.last.detail
    assert harness.clock.now - start >= harness.settings.health_timeouts["frontend"]


def test_rollback_drill_takes_the_real_rollback_path(harness: Harness) -> None:
    assert harness.run(rollback_drill=True) == 1
    host = harness.host
    assert host.count(COMPOSE_UP) == 2 and host.running["bot"][1] == OLD_B
    row = harness.ledger.last
    assert (row.outcome, row.backend_digest) == ("rolled_back", NEW_B)
    assert "unhealthy:rollback_drill" in row.detail
    # The drilled digest then deploys for real only on an explicit retry.
    assert harness.run() == 0 and harness.ledger.last.outcome == "rolled_back"
    assert harness.run(retry=True) == 0 and harness.ledger.last.outcome == "deployed"


@pytest.mark.parametrize("setup", ["pending_migration", "no_previous"])
def test_rollback_drill_is_refused_when_it_could_not_roll_back(harness: Harness, setup: str) -> None:
    if setup == "pending_migration":
        harness.host.current = ("h1",)
    else:
        harness.ledger.rows.clear()
    rows = len(harness.ledger.rows)
    assert harness.run(rollback_drill=True) == 1
    assert harness.host.count(COMPOSE_UP) == 0 and harness.host.count(BACKUP) == 0
    assert len(harness.ledger.rows) == rows


# --------------------------------------------------------------------------- discovery / ledger


def test_release_not_yet_fully_published_is_retried_and_alerted_once(harness: Harness) -> None:
    del harness.registry.tags[(FRONTEND, f"sha-{REV_NEW}")]
    rows = len(harness.ledger.rows)
    assert harness.run() == 1
    assert harness.host.calls == [] and harness.notices == []
    harness.clock.now += harness.settings.discovery_alert_after + 1
    assert harness.run() == 1
    assert [level for level, _ in harness.notices] == ["warning"]
    assert harness.run() == 1
    assert len(harness.notices) == 1
    assert len(harness.ledger.rows) == rows
    harness.registry.tags[(FRONTEND, f"sha-{REV_NEW}")] = NEW_F
    assert harness.run() == 0
    assert harness.notices[1][0] == "info"
    assert not (harness.settings.state_dir / "discovery-failure.json").exists()


def test_main_tag_and_revision_tag_must_agree(harness: Harness) -> None:
    harness.registry.tags[(BACKEND, f"sha-{REV_NEW}")] = "sha256:" + "9" * 64
    assert harness.run() == 1
    assert harness.host.calls == []


def test_ledger_write_failure_is_reported_not_swallowed(harness: Harness) -> None:
    harness.ledger.fail_append = True
    assert harness.run() == 1
    level, text = harness.notices[-1]
    assert level == "critical" and "LEDGER WRITE FAILED" in text


def test_unexpected_exception_alerts_instead_of_dying_silently(harness: Harness) -> None:
    def explode(repository: str, tag: str) -> str:
        raise RuntimeError("boom")

    harness.registry.resolve = explode  # type: ignore[method-assign]
    assert harness.run() == 1
    assert harness.notices[-1][0] == "critical"


def test_dry_run_reports_the_plan_and_changes_nothing(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    harness.host.current = ("h1",)
    harness.host.foreign = ["bfx-bot", "bfx-webapi", "bfx-frontend"]
    rows = len(harness.ledger.rows)
    assert harness.run(dry_run=True) == 0
    host = harness.host
    assert host.count(BACKUP) == 0 and host.count(_alembic("upgrade", "head")) == 0
    assert host.count(COMPOSE_UP) == 0 and host.stopped == []
    assert len(harness.ledger.rows) == rows and harness.notices == []
    plan = json.loads(capsys.readouterr().out)
    assert plan["migrations_pending"] is True and plan["backup_before_migration"] is True
    assert plan["change_class"] == "standard" and plan["rollback_target"] == REV_OLD
    assert plan["blockers"] == [f"foreign_container_holds_name:{n}"
                                for n in ("bfx-bot", "bfx-webapi", "bfx-frontend")]


# --------------------------------------------------------------------------- inspection unit


def _violations(record: dict[str, Any], service: str = "frontend") -> list[str]:
    backend = service != "frontend"
    return list(bfx.container_violations(
        record, service=service, image=f"{BACKEND if backend else FRONTEND}@{NEW_F}",
        digest=NEW_F, revision=REV_NEW, klass="standard", network="bfx_default"))


@pytest.mark.parametrize("service", ["bot", "webapi", "frontend"])
def test_compliant_container_has_no_violations(service: str) -> None:
    image = f"{BACKEND if service != 'frontend' else FRONTEND}@{NEW_F}"
    assert _violations(inspect_record(service, image, NEW_F, REV_NEW, "standard"), service) == []


@pytest.mark.parametrize(("mutate", "violation"), [
    (lambda r: r.__setitem__("Name", "/other"), "name"),
    (lambda r: r["Config"].__setitem__("Image", f"{FRONTEND}:main"), "image"),
    (lambda r: r["Config"].__setitem__("User", "root"), "user"),
    (lambda r: r["Config"].__setitem__("WorkingDir", "/"), "workdir"),
    (lambda r: r["Config"].__setitem__("Cmd", ["sh"]), "command"),
    (lambda r: r["Config"].__setitem__("Entrypoint", ["docker-entrypoint.sh"]), "entrypoint"),
    (lambda r: r["HostConfig"].__setitem__("ReadonlyRootfs", False), "read_only"),
    (lambda r: r["HostConfig"].__setitem__("Privileged", True), "privileged"),
    (lambda r: r["HostConfig"].__setitem__("CapAdd", ["NET_ADMIN"]), "cap_add"),
    (lambda r: r["HostConfig"].__setitem__("CapDrop", []), "cap_drop"),
    (lambda r: r["HostConfig"].__setitem__("SecurityOpt", []), "no_new_privileges"),
    (lambda r: r["NetworkSettings"]["Networks"].__setitem__("bridge", {}), "network"),
    (lambda r: r.__setitem__("Mounts", [{"Type": "bind"}]), "mounts"),
    (lambda r: r["HostConfig"].__setitem__("PortBindings", {"3000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3001"}]}), "ports"),
    (lambda r: r["HostConfig"].__setitem__("RestartPolicy", {"Name": "no"}), "restart"),
    (lambda r: r["Config"]["Env"].append("NODE_OPTIONS=--require /tmp/x"), "injection_env"),
    (lambda r: r["Config"]["Env"].append("LD_PRELOAD=/tmp/x.so"), "injection_env"),
    (lambda r: r["Config"].__setitem__("Env", [e for e in r["Config"]["Env"] if not e.startswith("BFX_IMAGE_DIGEST")]), "identity_env"),
])
def test_each_missing_hardening_property_is_a_violation(
    mutate: Callable[[dict[str, Any]], None], violation: str,
) -> None:
    record = inspect_record("frontend", f"{FRONTEND}@{NEW_F}", NEW_F, REV_NEW, "standard")
    mutate(record)
    assert violation in _violations(record)


def test_backend_specific_hardening() -> None:
    image = f"{BACKEND}@{NEW_F}"
    record = inspect_record("bot", image, NEW_F, REV_NEW, "standard")
    record["NetworkSettings"]["Networks"]["bfx_default"] = {"Aliases": ["bot"], "DNSNames": ["x"]}
    record["Config"]["Env"] = [e for e in record["Config"]["Env"] if not e.startswith("PYTHONDONTWRITEBYTECODE")]
    record["HostConfig"]["PortBindings"] = {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
    assert set(_violations(record, "bot")) == {"alias", "bytecode_env", "ports"}


# --------------------------------------------------------------------------- registry client


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def _sha(body: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(body).hexdigest()


class FakeTransport:
    def __init__(self, routes: dict[str, bytes], *, token: str = "tok") -> None:
        self.routes = routes
        self.token = token
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str], credentials: Mapping[str, str],
                 timeout: float) -> Any:
        self.requests.append((url, dict(headers), dict(credentials)))
        if url.startswith("https://ghcr.io/token?"):
            return bfx.HttpResponse(200, {}, json.dumps({"token": self.token}).encode())
        if credentials.get("Authorization") != f"Bearer {self.token}":
            return bfx.HttpResponse(401, {"www-authenticate": 'Bearer realm="https://ghcr.io/token",'
                                          'service="ghcr.io",scope="repository:x:pull"'}, b"")
        path = url.removeprefix("https://ghcr.io/v2/will413028/bfx-funding-bot-backend/")
        if path not in self.routes:
            return bfx.HttpResponse(404, {}, b"")
        body = self.routes[path]
        return bfx.HttpResponse(200, {"docker-content-digest": _sha(body)}, body)


def _image_routes(revision: str) -> tuple[dict[str, bytes], str]:
    config = json.dumps({"config": {"Labels": {"org.opencontainers.image.revision": revision}}}).encode()
    child = _manifest_bytes({"schemaVersion": 2, "config": {"digest": _sha(config)}, "layers": []})
    attestation = _manifest_bytes({"schemaVersion": 2, "config": {"digest": _sha(b"{}")}})
    index = _manifest_bytes({"schemaVersion": 2, "manifests": [
        {"digest": _sha(child), "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": _sha(attestation), "platform": {"os": "unknown", "architecture": "unknown"},
         "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
    ]})
    routes = {"manifests/main": index, f"manifests/{_sha(index)}": index,
              f"manifests/{_sha(child)}": child, f"blobs/{_sha(config)}": config}
    return routes, _sha(index)


def test_registry_resolves_main_and_reads_the_revision_label_with_verified_content() -> None:
    routes, index_digest = _image_routes(REV_NEW)
    transport = FakeTransport(routes)
    client = bfx.RegistryClient(credentials=("will413028", "ghp_secret"), transport=transport)
    assert client.resolve(BACKEND, "main") == index_digest
    assert client.revision(BACKEND, index_digest) == REV_NEW
    token_calls = [r for r in transport.requests if r[0].startswith("https://ghcr.io/token")]
    assert len(token_calls) == 1
    assert token_calls[0][2]["Authorization"].startswith("Basic ")
    # Credentials only ever travel as non-redirectable headers.
    assert all("Authorization" not in headers for _, headers, _ in transport.requests)


def test_registry_rejects_content_that_does_not_match_its_digest() -> None:
    routes, index_digest = _image_routes(REV_NEW)
    routes[f"manifests/{index_digest}"] = routes["manifests/main"] + b" "
    client = bfx.RegistryClient(credentials=None, transport=FakeTransport(routes))
    with pytest.raises(bfx.DiscoveryError, match="digest_mismatch"):
        client.revision(BACKEND, index_digest)


def test_registry_never_sends_credentials_to_a_foreign_token_realm() -> None:
    sent: list[dict[str, str]] = []

    def transport(url: str, headers: Mapping[str, str], credentials: Mapping[str, str],
                  timeout: float) -> Any:
        sent.append(dict(credentials))
        return bfx.HttpResponse(401, {"www-authenticate": 'Bearer realm="https://evil.example/token"'}, b"")

    client = bfx.RegistryClient(credentials=("u", "secret"), transport=transport)
    with pytest.raises(bfx.DiscoveryError, match="challenge_rejected"):
        client.resolve(BACKEND, "main")
    assert sent == [{}]


def test_missing_tag_is_a_discovery_error_not_a_failure() -> None:
    client = bfx.RegistryClient(credentials=None, transport=FakeTransport({}))
    with pytest.raises(bfx.DiscoveryError, match="registry_not_found"):
        client.resolve(BACKEND, f"sha-{REV_NEW}")


# --------------------------------------------------------------------------- psql ledger argv


def test_psql_ledger_passes_values_as_psql_variables_never_as_sql() -> None:
    seen: dict[str, Any] = {}

    def runner(argv: Sequence[str], *, timeout: float, input_text: str | None = None,
               env: Mapping[str, str] | None = None) -> Any:
        seen["argv"], seen["sql"] = list(argv), input_text
        return bfx.CommandResult(0, "7\n", "")

    ledger = bfx.PsqlLedger(runner, container="bfx-postgres", db_user="bfx", db_name="bfx")
    detail = "unhealthy:x'; DROP TABLE deployments; --"
    ledger_id = ledger.append(bfx.LedgerEntry(
        started_at="2026-09-25T00:00:00+00:00", finished_at="2026-09-25T00:05:00+00:00",
        source_revision=REV_NEW, backend_digest=NEW_B, frontend_digest=NEW_F,
        change_class="material", migrations_applied=True, outcome="failed", detail=detail))
    assert ledger_id == 7
    assert seen["argv"][:6] == ["docker", "exec", "-i", "--user", "postgres", "bfx-postgres"]
    assert f"detail={detail}" in seen["argv"]
    assert detail not in seen["sql"] and ":'detail'" in seen["sql"]


def test_load_registry_credentials(tmp_path: Path) -> None:
    assert bfx.load_registry_credentials(tmp_path / "absent.env", lambda p: None) is None
    path = tmp_path / "ghcr.env"
    path.write_text("GHCR_USERNAME=will413028\nGHCR_TOKEN=ghp_x\n")
    assert bfx.load_registry_credentials(path, lambda p: None) == ("will413028", "ghp_x")
    path.write_text("GHCR_USERNAME=will413028\n")
    with pytest.raises(bfx.DeployError, match="incomplete"):
        bfx.load_registry_credentials(path, lambda p: None)


def test_secret_file_must_be_owned_0600_regular_and_under_root_controlled_dirs(tmp_path: Path) -> None:
    uid = __import__("os").getuid()
    secret = tmp_path / "bot.env"
    secret.write_text("A=1\n")
    secret.chmod(0o640)
    with pytest.raises(bfx.DeployError, match="mode_not_0600"):
        bfx.protected_secret_file(secret, owner_uid=uid)
    secret.chmod(0o600)
    with pytest.raises(bfx.DeployError, match="not_root_controlled"):
        bfx.protected_secret_file(secret, owner_uid=uid + 1)
    link = tmp_path / "link.env"
    link.symlink_to(secret)
    with pytest.raises(bfx.DeployError, match="secret_file_missing"):
        bfx.protected_secret_file(link, owner_uid=uid)
    with pytest.raises(bfx.DeployError, match="secret_file_missing"):
        bfx.protected_secret_file(tmp_path / "absent.env", owner_uid=uid)
    # A correct file under a world-writable or foreign-owned ancestor (the test's
    # temporary directory lives under / and /tmp) is still refused.
    with pytest.raises(bfx.DeployError, match="not_root_controlled"):
        bfx.protected_secret_file(secret, owner_uid=uid)
