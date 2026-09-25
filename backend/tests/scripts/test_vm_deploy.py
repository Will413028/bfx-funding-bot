"""bfx-deploy decision flow against a simulated VM (no Docker, no network).

The simulated host answers the exact argv bfx-deploy issues (git through
runuser, docker pull/run/compose/inspect/exec/stop, backup.sh, systemctl, uv)
and keeps just enough state -- schema revision, running images, health by
digest, git worktrees -- to prove the ordering and fail-safe rules: the bot is
stopped before a migration and stays stopped if anything after that fails, a
backup and a restore test with the target's DR scripts precede the migration,
the `started` row precedes any container change, rollback only when no
migration ran, and the target's tooling is installed only once it deployed.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
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
OLD_ATTEMPT = "11111111-1111-4111-8111-111111111111"
CI_RUN = "https://github.com/Will413028/bfx-funding-bot/actions/runs/123456789"
RULES_TEXT = (ROOT / "deploy/change-class.yaml").read_text(encoding="utf-8")
COMPOSE_TEXT = (ROOT / "deploy/vm/docker-compose.app.yml").read_text(encoding="utf-8")
LIVE_ENV_TEXT = (ROOT / "deploy/vm/live.env").read_text(encoding="utf-8")
TOOLING = {
    str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
    for base in ("deploy/vm/ops", "deploy/vm/systemd")
    for path in sorted((ROOT / base).rglob("*"))
    if path.is_file() and "__pycache__" not in path.parts and ".venv" not in path.parts
}


# --------------------------------------------------------------------------- fakes


class FakeRegistry:
    def __init__(self) -> None:
        self.refs = {
            f"{BACKEND}:main": NEW_B, f"{BACKEND}:sha-{REV_NEW}": NEW_B,
            f"{FRONTEND}:sha-{REV_NEW}": NEW_F,
            f"{BACKEND}:sha-{REV_OLD}": OLD_B, f"{FRONTEND}:sha-{REV_OLD}": OLD_F,
        }
        self.labels: dict[str, dict[str, str]] = {
            NEW_B: {bfx.REVISION_LABEL: REV_NEW, bfx.CI_RUN_LABEL: CI_RUN},
            NEW_F: {bfx.REVISION_LABEL: REV_NEW},
            OLD_B: {bfx.REVISION_LABEL: REV_OLD}, OLD_F: {bfx.REVISION_LABEL: REV_OLD},
        }
        self.inspected: list[str] = []

    def inspect(self, reference: str) -> Any:
        self.inspected.append(reference)
        if reference not in self.refs:
            raise bfx.DiscoveryError(f"registry_not_found:{reference}")
        digest = self.refs[reference]
        return bfx.ImageInfo(digest=digest, labels=self.labels.get(digest, {}))


class FakeLedger:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.rows: list[Any] = []
        self.fail_append: set[str] = set()  # outcomes whose append fails

    def seed(self, revision: str, backend: str, frontend: str, outcome: str,
             klass: str = "standard", attempt_id: str = OLD_ATTEMPT) -> None:
        self.rows.append(bfx.LedgerEntry(
            attempt_id=attempt_id,
            started_at="2026-09-24T00:00:00+00:00", finished_at="2026-09-24T00:01:00+00:00",
            source_revision=revision, backend_digest=backend, frontend_digest=frontend,
            change_class=klass, migrations_applied=False, outcome=outcome, detail="seed"))

    def _row(self, index: int) -> Any:
        entry = self.rows[index]
        return bfx.LedgerRow(id=index + 1, attempt_id=entry.attempt_id,
                             source_revision=entry.source_revision,
                             backend_digest=entry.backend_digest,
                             frontend_digest=entry.frontend_digest,
                             change_class=entry.change_class, outcome=entry.outcome,
                             migrations_applied=entry.migrations_applied, ci_run=entry.ci_run)

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
        if entry.outcome in self.fail_append or "*" in self.fail_append:
            raise bfx.CommandError("ledger_psql_exit_3")
        self.rows.append(entry)
        return len(self.rows)

    @property
    def last(self) -> Any:
        return self.rows[-1]

    def attempt(self, attempt_id: str) -> list[str]:
        return [row.outcome for row in self.rows if row.attempt_id == attempt_id]


def inspect_record(service: str, running: tuple[str, str, str, str, str]) -> dict[str, Any]:
    image, digest, revision, klass, deployment_id = running
    env = [f"BFX_IMAGE_DIGEST={digest}", f"BFX_SOURCE_REVISION={revision}",
           f"BFX_CHANGE_CLASS={klass}", f"BFX_DEPLOYMENT_ID={deployment_id}", "PATH=/usr/bin"]
    return {
        "Name": "/" + bfx.CONTAINERS[service],
        "Config": {"Image": image, "Env": env,
                   "Labels": {"com.docker.compose.project": "bfx-app"}},
    }


@dataclass
class FakeHost:
    """Answers bfx-deploy's argv like the VM would; records every call."""

    mirror: Path
    dr_root: Path
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
    restore_test_ok: bool = True
    uv_ok: bool = True
    stop_ok: bool = True
    compose_fail: set[str] = field(default_factory=set)
    unhealthy: set[str] = field(default_factory=set)
    tampered: dict[str, Callable[[dict[str, Any]], None]] = field(default_factory=dict)
    foreign: list[str] = field(default_factory=list)
    running: dict[str, tuple[str, str, str, str, str]] = field(default_factory=dict)
    stopped: list[str] = field(default_factory=list)
    worktrees: dict[str, str] = field(default_factory=dict)
    docker_configs: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for revision in (REV_OLD, REV_NEW):
            self.files[(revision, bfx.COMPOSE_PATH)] = COMPOSE_TEXT
            self.files[(revision, bfx.LIVE_ENV_PATH)] = LIVE_ENV_TEXT
            self.files[(revision, "deploy/change-class.yaml")] = RULES_TEXT
            for path, text in TOOLING.items():
                self.files[(revision, path)] = text
        self.diffs.setdefault((REV_OLD, REV_NEW), ["docs/runbooks/offsite-dr.md"])
        self.start_release(REV_OLD, OLD_B, OLD_F, "standard", OLD_ATTEMPT)

    def start_release(self, revision: str, backend: str, frontend: str, klass: str,
                      deployment_id: str) -> None:
        for service in ("bot", "webapi"):
            self.running[service] = (f"{BACKEND}@{backend}", backend, revision, klass, deployment_id)
        self.running["frontend"] = (f"{FRONTEND}@{frontend}", frontend, revision, klass, deployment_id)

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
            assert call[5] == "-C"
            return self._git(Path(call[6]), call[7:])
        if call[:4] == ("runuser", "-u", "ubuntu", "--") and call[4].endswith("backup.sh"):
            assert call[5:] == ("--type", "diff")
            return ok if self.backup_ok else bfx.CommandResult(2, "", "backup_evidence_unavailable")
        if call[:2] == ("systemctl", "start"):
            assert call[2].startswith("bfx-restore-test@") and call[2].endswith(".service")
            return ok if self.restore_test_ok else bfx.CommandResult(1, "", "Job failed")
        if call == ("systemctl", "daemon-reload"):
            return ok
        if call[:2] == ("uv", "sync"):
            assert env is not None
            if not self.uv_ok:
                return bfx.CommandResult(2, "", "no network")
            venv = Path(env["UV_PROJECT_ENVIRONMENT"]) / "bin"
            venv.mkdir(parents=True)
            (venv / "python").write_text("")
            return ok
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
            if not self.stop_ok:
                return bfx.CommandResult(1, "", "daemon unreachable")
            if "bot" not in self.running:
                return bfx.CommandResult(1, "", "Error response from daemon: No such container: bfx-bot")
            del self.running["bot"]
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

    def _git(self, where: Path, args: tuple[str, ...]) -> Any:
        if where != self.mirror:
            assert where.parent == self.dr_root, where
            if args == ("rev-parse", "HEAD"):
                return bfx.CommandResult(0, self.worktrees.get(str(where), "") + "\n", "")
            if args[:2] == ("status", "--porcelain"):
                return bfx.CommandResult(0, "", "")
            raise AssertionError(f"unexpected git in checkout: {args}")
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
        if args[:4] == ("ls-tree", "-r", "-z", "--name-only"):
            revision, prefixes = args[4], args[6:]
            listed = [path for (rev, path) in self.files if rev == revision
                      and any(path.startswith(prefix + "/") for prefix in prefixes)]
            return bfx.CommandResult(0, "".join(p + "\0" for p in sorted(listed)), "")
        if args[:4] == ("worktree", "add", "--detach", "--force"):
            path, revision = Path(args[4]), args[5]
            assert revision in self.commits
            path.mkdir(parents=True)
            self.worktrees[str(path)] = revision
            return bfx.CommandResult(0, "", "")
        if args[:3] == ("worktree", "remove", "--force"):
            shutil.rmtree(args[3])
            self.worktrees.pop(args[3], None)
            return bfx.CommandResult(0, "", "")
        if args == ("worktree", "prune"):
            return bfx.CommandResult(0, "", "")
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
        assert call[6:9] == ("up", "--detach", "--no-deps")
        assert call[-3:] == ("bot", "webapi", "frontend")
        assert env is not None
        if env["BFX_BACKEND_DIGEST"] in self.compose_fail:
            return bfx.CommandResult(1, "", "compose failed")
        backend = (env["BFX_BACKEND_IMAGE"], env["BFX_BACKEND_DIGEST"])
        frontend = (env["BFX_FRONTEND_IMAGE"], env["BFX_FRONTEND_DIGEST"])
        identity = (env["BFX_SOURCE_REVISION"], env["BFX_CHANGE_CLASS"], env["BFX_DEPLOYMENT_ID"])
        for service in ("bot", "webapi"):
            self.running[service] = (*backend, *identity)
        self.running["frontend"] = (*frontend, *identity)
        return bfx.CommandResult(0, "", "")

    def _inspect(self, call: tuple[str, ...]) -> Any:
        if "--format" in call:
            lines = [f"/{bfx.CONTAINERS[s]} 0 true" for s in bfx.SERVICES if s in self.running]
            return bfx.CommandResult(0, "\n".join(lines), "")
        records = []
        for service in bfx.SERVICES:
            if service not in self.running:
                return bfx.CommandResult(1, "[]", "no such container")
            record = inspect_record(service, self.running[service])
            digest = self.running[service][1]
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
    units = tmp_path / "units"
    units.mkdir()
    settings = bfx.Settings(
        mirror=mirror, mirror_user="ubuntu", runtime_dir=runtime,
        state_dir=tmp_path / "state", lock_file=tmp_path / "deploy.lock",
        dr_root=tmp_path / "bfx-releases", ops_root=tmp_path / "bfx-ops", unit_dir=units,
        uv="uv", backup_user="ubuntu",
    )
    ledger = FakeLedger()
    ledger.seed(REV_OLD, OLD_B, OLD_F, "deployed")
    return Harness(host=FakeHost(mirror=mirror, dr_root=settings.dr_root), registry=FakeRegistry(),
                   ledger=ledger, clock=FakeClock(), notices=[], settings=settings)


def _is(prefix: tuple[str, ...]) -> Callable[[tuple[str, ...]], bool]:
    return lambda call: call[:len(prefix)] == prefix


def _alembic(*args: str) -> Callable[[tuple[str, ...]], bool]:
    return lambda call: call[:2] == ("docker", "run") and call[-len(args):] == args


BACKUP = lambda call: len(call) > 4 and call[4].endswith("backup.sh")  # noqa: E731
RESTORE_TEST = _is(("systemctl", "start"))
COMPOSE_UP = _is(("docker", "compose"))
STOP_BOT = _is(("docker", "stop"))
PULL = _is(("docker", "pull"))
UV_SYNC = _is(("uv", "sync"))


def _started(harness: Harness) -> list[Any]:
    return [row for row in harness.ledger.rows if row.outcome == "started"]


# --------------------------------------------------------------------------- no-op


def test_nothing_happens_when_main_already_runs(harness: Harness) -> None:
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, "deployed", attempt_id="n")
    rows = len(harness.ledger.rows)
    assert harness.run() == 0
    assert harness.host.calls == []
    assert (harness.notices, len(harness.ledger.rows)) == ([], rows)


@pytest.mark.parametrize("outcome", ["rolled_back", "failed", "started"])
def test_a_digest_already_attempted_is_not_retried_every_tick(harness: Harness, outcome: str) -> None:
    # `started` without a terminal row: the attempt was interrupted (crash, kill).
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, outcome, attempt_id="n")
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
    assert host.count(BACKUP) == 0 and host.count(STOP_BOT) == 0
    assert host.count(_alembic("upgrade", "head")) == 0
    assert host.index(PULL) < host.index(_alembic("current")) < host.index(COMPOSE_UP)
    env = host.envs[host.index(COMPOSE_UP)]
    assert env is not None
    assert env["BFX_BACKEND_IMAGE"] == f"{BACKEND}@{NEW_B}"
    assert env["BFX_FRONTEND_IMAGE"] == f"{FRONTEND}@{NEW_F}"
    assert (env["BFX_SOURCE_REVISION"], env["BFX_CHANGE_CLASS"]) == (REV_NEW, "standard")
    # Compose reads the env files bfx-deploy validated, not a separate default.
    assert env["BFX_RUNTIME_DIR"] == str(harness.settings.runtime_dir)
    row = harness.ledger.last
    assert (row.outcome, row.change_class, row.migrations_applied) == ("deployed", "standard", False)
    assert (row.backend_digest, row.frontend_digest, row.source_revision) == (NEW_B, NEW_F, REV_NEW)
    assert harness.notices[-1][0] == "info"
    assert host.stopped == []
    assert host.count(_is(("docker", "tag", f"{BACKEND}@{NEW_B}", "bfx-bot:local"))) == 1


def test_started_row_precedes_the_containers_and_names_their_deployment_id(harness: Harness) -> None:
    ledger_rows_at_compose: list[int] = []
    original = harness.host._compose

    def compose(call: tuple[str, ...], env: Mapping[str, str] | None) -> Any:
        ledger_rows_at_compose.append(len(harness.ledger.rows))
        return original(call, env)

    harness.host._compose = compose  # type: ignore[method-assign]
    assert harness.run() == 0
    started, finished = harness.ledger.rows[-2], harness.ledger.rows[-1]
    assert (started.outcome, finished.outcome) == ("started", "deployed")
    assert ledger_rows_at_compose == [len(harness.ledger.rows) - 1]  # started row already there
    assert started.attempt_id == finished.attempt_id
    assert started.finished_at is None and finished.finished_at is not None
    assert (started.source_revision, started.backend_digest, started.frontend_digest,
            started.change_class, started.started_at) == (
        finished.source_revision, finished.backend_digest, finished.frontend_digest,
        finished.change_class, finished.started_at)
    env = harness.host.envs[harness.host.index(COMPOSE_UP)]
    assert env is not None and env["BFX_DEPLOYMENT_ID"] == started.attempt_id
    assert harness.host.running["bot"][4] == started.attempt_id
    assert started.ci_run == finished.ci_run == CI_RUN


def test_invalid_ci_run_label_is_not_recorded(harness: Harness) -> None:
    harness.registry.labels[NEW_B][bfx.CI_RUN_LABEL] = "https://evil.example/runs/1"
    assert harness.run() == 0
    assert harness.ledger.last.ci_run is None


def test_started_row_that_cannot_be_written_changes_no_container(harness: Harness) -> None:
    harness.ledger.fail_append = {"started"}
    assert harness.run() == 1
    assert harness.host.count(COMPOSE_UP) == 0
    row = harness.ledger.last
    assert row.outcome == "failed" and "ledger_started_unrecorded" in row.detail
    assert harness.host.running["bot"][1] == OLD_B


def test_pending_migration_stops_the_bot_then_backs_up_restore_tests_and_migrates(harness: Harness) -> None:
    harness.host.current = ("h1",)
    assert harness.run() == 0
    host = harness.host
    assert (host.index(STOP_BOT) < host.index(BACKUP) < host.index(RESTORE_TEST)
            < host.index(_alembic("upgrade", "head")) < host.index(COMPOSE_UP))
    # The target release's DR scripts, from a clean checkout of <rev>.
    checkout = harness.settings.dr_root / REV_NEW
    assert host.calls[host.index(BACKUP)][4] == str(checkout / "deploy/vm/pgbackrest/backup.sh")
    assert host.calls[host.index(RESTORE_TEST)] == ("systemctl", "start", f"bfx-restore-test@{REV_NEW}.service")
    assert host.worktrees[str(checkout)] == REV_NEW
    started = _started(harness)[-1]
    assert started.migrations_applied is True
    assert harness.ledger.last.migrations_applied is True
    assert harness.ledger.last.outcome == "deployed"
    assert host.running["bot"][1] == NEW_B


def test_material_path_in_the_diff_marks_the_deployment_material(harness: Harness) -> None:
    harness.host.diffs[(REV_OLD, REV_NEW)] = ["docs/a.md", "backend/src/bfx_funding_bot/main.py"]
    assert harness.run() == 0
    env = harness.host.envs[harness.host.index(COMPOSE_UP)]
    assert env is not None and env["BFX_CHANGE_CLASS"] == "material"
    assert _started(harness)[-1].change_class == "material"
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


# --------------------------------------------------------------------------- tooling


def test_successful_deploy_installs_its_own_tooling_for_the_next_run(harness: Harness) -> None:
    assert harness.run() == 0
    ops = harness.settings.ops_root
    release = ops / "releases" / REV_NEW
    assert (ops / "current").resolve() == release.resolve()
    assert (release / "ops/bfx_deploy.py").read_text() == TOOLING["deploy/vm/ops/bfx_deploy.py"]
    assert (release / "ops/uv.lock").is_file() and (release / "ops/.venv/bin/python").is_file()
    assert (release / ".installed").read_text().strip() == REV_NEW
    sync = harness.host.calls[harness.host.index(UV_SYNC)]
    assert "--frozen" in sync and sync[-1] == str(ops / "releases" / f".{REV_NEW}.staging" / "ops")
    managed = [line for line in TOOLING["deploy/vm/systemd/managed-units"].splitlines()
               if line and not line.startswith("#")]
    assert sorted(p.name for p in harness.settings.unit_dir.iterdir()) == sorted(managed)
    assert "bfx-halt-watch.service" not in managed
    assert harness.host.index(UV_SYNC) < harness.host.index(_is(("systemctl", "daemon-reload")))
    dr_current = harness.settings.dr_root / "current"
    assert os.readlink(dr_current) == REV_NEW
    assert harness.host.worktrees[str(harness.settings.dr_root / REV_NEW)] == REV_NEW
    # Everything a non-root DR unit executes is readable by it.
    assert oct((release / "ops").stat().st_mode & 0o777) == "0o755"
    assert oct((release / "ops/bfx_restore_test.py").stat().st_mode & 0o777) == "0o644"
    assert harness.ledger.last.detail.endswith("ok")


def test_tooling_is_never_installed_from_a_release_that_did_not_deploy(harness: Harness) -> None:
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    assert not (harness.settings.ops_root / "current").exists()
    assert harness.host.count(UV_SYNC) == 0
    assert list(harness.settings.unit_dir.iterdir()) == []


def test_tooling_install_failure_is_a_warning_and_keeps_the_previous_tooling(harness: Harness) -> None:
    harness.host.uv_ok = False
    assert harness.run() == 0
    row = harness.ledger.last
    assert row.outcome == "deployed" and "tooling_install_failed:tooling_uv_sync_failed" in row.detail
    assert not (harness.settings.ops_root / "current").exists()
    assert harness.notices[-1][0] == "warning"


def test_old_releases_and_dr_checkouts_are_pruned_keeping_deployed_and_previous(harness: Harness) -> None:
    stale = "c" * 40
    harness.host.commits.add(stale)
    for rev in (stale, REV_OLD):
        (harness.settings.ops_root / "releases" / rev).mkdir(parents=True)
        harness.host._git(harness.host.mirror, ("worktree", "add", "--detach", "--force",
                                                str(harness.settings.dr_root / rev), rev))
    assert harness.run() == 0
    assert sorted(p.name for p in (harness.settings.ops_root / "releases").iterdir()) == sorted([REV_OLD, REV_NEW])
    assert sorted(harness.host.worktrees) == sorted(
        str(harness.settings.dr_root / rev) for rev in (REV_OLD, REV_NEW))


# --------------------------------------------------------------------------- refusals


def test_revision_not_on_main_is_refused_before_pulling(harness: Harness) -> None:
    harness.host.on_main.discard(REV_NEW)
    assert harness.run() == 1
    assert harness.host.count(PULL) == 0
    assert "precheck:revision_not_on_main" in harness.ledger.last.detail
    assert _started(harness) == []  # an attempt that changed nothing has only its terminal row


def test_old_containers_holding_the_names_block_the_deploy(harness: Harness) -> None:
    harness.host.foreign = ["bfx-bot"]
    assert harness.run() == 1
    assert harness.host.count(PULL) == 0 and harness.host.count(COMPOSE_UP) == 0
    assert "foreign_container_holds_name:bfx-bot" in harness.ledger.last.detail


@pytest.mark.parametrize(("file", "text", "code"), [
    ("bot.env", "SECRET=abc$def\n", "env_file_value_ambiguous_for_compose:bot.env:SECRET"),
    ("bot.env", 'SECRET="quoted"\n', "env_file_value_ambiguous_for_compose:bot.env:SECRET"),
    ("bot.env", "SECRET=value #comment\n", "env_file_value_ambiguous_for_compose:bot.env:SECRET"),
    ("bot.env", "not a line\n", "env_file_invalid_line:bot.env"),
    ("webapi.env", "DATABASE_URL=x\n", "env_file_duplicate_key:webapi.env:DATABASE_URL"),
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
    assert not {"BFX_IMAGE_DIGEST", "BFX_SOURCE_REVISION", "BFX_CHANGE_CLASS", "BFX_DEPLOYMENT_ID"} & set(values)


def test_bot_that_cannot_be_stopped_blocks_the_migration(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.stop_ok = False
    assert harness.run() == 1
    assert harness.host.count(BACKUP) == 0 and harness.host.count(_alembic("upgrade", "head")) == 0
    assert "bot_stop_failed; nothing migrated" in harness.ledger.last.detail


def test_absent_bot_does_not_block_a_first_migration(harness: Harness) -> None:
    harness.host.current = ("h1",)
    del harness.host.running["bot"]
    assert harness.run() == 0
    assert harness.ledger.last.outcome == "deployed"


@pytest.mark.parametrize(("setup", "code", "migrated"), [
    ("backup", "backup_failed", False),
    ("restore", "restore_test_failed(migration_pending)", False),
    ("fail_unchanged", "migration_failed", False),
    ("fail_partial", "migration_partial_or_unverified", True),
])
def test_any_failure_after_the_bot_stopped_keeps_it_stopped(
    harness: Harness, setup: str, code: str, migrated: bool,
) -> None:
    harness.host.current = ("h1",)
    if setup == "backup":
        harness.host.backup_ok = False
    elif setup == "restore":
        harness.host.restore_test_ok = False
    else:
        harness.host.upgrade_result = setup
    assert harness.run() == 1
    host = harness.host
    assert host.stopped == ["bfx-bot"] and "bot" not in host.running
    assert host.count(COMPOSE_UP) == 0 and _started(harness) == []
    if setup in {"backup", "restore"}:
        assert host.count(_alembic("upgrade", "head")) == 0
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", migrated)
    assert code in row.detail and "bot stopped until a release deploys" in row.detail
    assert harness.notices[-1][0] == "critical"


@pytest.mark.parametrize("path", [
    "deploy/vm/pgbackrest/pgbackrest.conf",
    "deploy/vm/pgbackrest/restore_drill.py",
    "deploy/vm/postgres/Dockerfile",
    "docker-compose.bot.yml",
    "docker-compose.dr.yml",
])
def test_dr_path_change_runs_the_targets_restore_test_before_deploying(harness: Harness, path: str) -> None:
    harness.host.diffs[(REV_OLD, REV_NEW)] = ["docs/a.md", path]
    assert harness.run() == 0
    host = harness.host
    assert host.count(BACKUP) == 0 and host.stopped == []   # no migration: bot keeps running
    assert host.index(RESTORE_TEST) < host.index(COMPOSE_UP)
    assert host.calls[host.index(RESTORE_TEST)][2] == f"bfx-restore-test@{REV_NEW}.service"


def test_dr_path_change_with_a_failing_restore_test_is_not_deployed(harness: Harness) -> None:
    harness.host.diffs[(REV_OLD, REV_NEW)] = ["deploy/vm/pgbackrest/pgbackrest.conf"]
    harness.host.restore_test_ok = False
    assert harness.run() == 1
    assert harness.host.count(COMPOSE_UP) == 0 and harness.host.stopped == []
    assert "restore_test_failed(dr_paths:deploy/vm/pgbackrest/pgbackrest.conf)" in harness.ledger.last.detail


def test_existing_clean_checkout_of_the_target_is_reused(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host._git(harness.host.mirror, ("worktree", "add", "--detach", "--force",
                                            str(harness.settings.dr_root / REV_NEW), REV_NEW))
    adds = harness.host.count(lambda c: c[7:9] == ("worktree", "add"))
    assert harness.run() == 0
    assert harness.host.count(lambda c: c[7:9] == ("worktree", "add")) == adds


@pytest.mark.parametrize("paths", [
    ["docs/a.md"],
    ["backend/src/bfx_funding_bot/main.py", "frontend/src/app/page.tsx"],
    ["deploy/vm/live.env", "deploy/vm/ops/bfx_deploy.py"],
])
def test_ordinary_release_without_migration_skips_the_restore_test(harness: Harness,
                                                                   paths: list[str]) -> None:
    harness.host.diffs[(REV_OLD, REV_NEW)] = paths
    assert harness.run() == 0
    assert harness.host.count(RESTORE_TEST) == 0


def test_unreadable_diff_runs_the_restore_test(harness: Harness) -> None:
    harness.host.diffs.pop((REV_OLD, REV_NEW))
    assert harness.run() == 0
    assert harness.host.count(RESTORE_TEST) == 1


def test_rules_file_problem_alone_does_not_trigger_a_restore_test(harness: Harness) -> None:
    harness.host.files[(REV_NEW, "deploy/change-class.yaml")] = "version: 9\n"
    assert harness.run() == 0
    assert harness.ledger.last.change_class == "material"
    assert harness.host.count(RESTORE_TEST) == 0


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
    # The previous release comes back under its own deployment id and class.
    assert (rollback_env["BFX_DEPLOYMENT_ID"], rollback_env["BFX_CHANGE_CLASS"]) == (OLD_ATTEMPT, "standard")
    assert Path(host.calls[ups[1]][5]).parent.name == REV_OLD
    assert host.running["bot"][1] == OLD_B and host.stopped == []
    row = harness.ledger.last
    assert (row.outcome, row.backend_digest) == ("rolled_back", NEW_B)
    assert harness.ledger.attempt(row.attempt_id) == ["started", "rolled_back"]
    assert f"rolled_back_to={REV_OLD}" in row.detail
    assert harness.notices[-1][0] == "warning"
    assert host.count(_is(("docker", "tag"))) == 0


def test_unhealthy_release_after_migration_stops_the_bot_without_rollback(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    host = harness.host
    assert host.count(COMPOSE_UP) == 1
    assert host.stopped == ["bfx-bot", "bfx-bot"]  # before the migration, and after the failed start
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


@pytest.mark.parametrize(("tamper", "problem"), [
    (lambda r: r["Config"].__setitem__("Image", f"{BACKEND}:main"), "bot:image"),
    (lambda r: r["Config"]["Labels"].__setitem__("com.docker.compose.project", "bfx"), "bot:project"),
    (lambda r: r["Config"].__setitem__("Env", [e for e in r["Config"]["Env"]
                                               if not e.startswith("BFX_DEPLOYMENT_ID")]), "bot:identity_env"),
])
def test_container_not_running_the_recorded_release_is_a_failed_start(
    harness: Harness, tamper: Callable[[dict[str, Any]], None], problem: str,
) -> None:
    harness.host.tampered[NEW_B] = tamper
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "rolled_back"
    assert f"container_release_mismatch:{problem}" in row.detail


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
    assert harness.host.count(STOP_BOT) == 0
    assert len(harness.ledger.rows) == rows


# --------------------------------------------------------------------------- recreate


def test_recreate_redeploys_the_running_release_through_the_ledger(harness: Harness) -> None:
    assert harness.run(recreate=True) == 0
    host = harness.host
    assert harness.registry.inspected == []   # the ledger names the release; no discovery
    compose = host.calls[host.index(COMPOSE_UP)]
    assert "--force-recreate" in compose
    env = host.envs[host.index(COMPOSE_UP)]
    assert env is not None
    assert (env["BFX_BACKEND_DIGEST"], env["BFX_FRONTEND_DIGEST"], env["BFX_SOURCE_REVISION"]) == (
        OLD_B, OLD_F, REV_OLD)
    assert env["BFX_CHANGE_CLASS"] == "standard"
    started, finished = harness.ledger.rows[-2:]
    assert (started.outcome, finished.outcome) == ("started", "deployed")
    assert env["BFX_DEPLOYMENT_ID"] == started.attempt_id == finished.attempt_id != OLD_ATTEMPT
    assert finished.detail.startswith("recreate; class=standard(recreate_same_release)")
    assert host.count(STOP_BOT) == 0 and host.count(BACKUP) == 0 and host.count(UV_SYNC) == 0


def test_recreate_can_be_raised_to_material(harness: Harness) -> None:
    assert harness.run(recreate=True, force_material=True) == 0
    assert harness.ledger.last.change_class == "material"


def test_recreate_that_fails_stops_the_bot_and_has_no_rollback(harness: Harness) -> None:
    harness.host.unhealthy = {OLD_B}
    assert harness.run(recreate=True) == 1
    assert harness.host.count(COMPOSE_UP) == 1 and harness.host.stopped == ["bfx-bot"]
    row = harness.ledger.last
    assert row.outcome == "failed" and "recreate has no rollback" in row.detail


def test_recreate_refuses_without_a_deployment_or_with_a_pending_schema(harness: Harness) -> None:
    harness.host.current = ("h1",)
    assert harness.run(recreate=True) == 1
    assert harness.host.count(COMPOSE_UP) == 0
    assert "precheck:schema_not_at_release_head" in harness.ledger.last.detail
    harness.ledger.rows.clear()
    assert harness.run(recreate=True) == 1
    assert harness.ledger.rows == []


# --------------------------------------------------------------------------- discovery / ledger


def test_release_not_yet_fully_published_is_retried_and_alerted_once(harness: Harness) -> None:
    del harness.registry.refs[f"{FRONTEND}:sha-{REV_NEW}"]
    rows = len(harness.ledger.rows)
    assert harness.run() == 1
    assert harness.host.calls == [] and harness.notices == []
    harness.clock.now += harness.settings.discovery_alert_after + 1
    assert harness.run() == 1
    assert [level for level, _ in harness.notices] == ["warning"]
    assert harness.run() == 1
    assert len(harness.notices) == 1
    assert len(harness.ledger.rows) == rows
    harness.registry.refs[f"{FRONTEND}:sha-{REV_NEW}"] = NEW_F
    assert harness.run() == 0
    assert harness.notices[1][0] == "info"
    assert not (harness.settings.state_dir / "discovery-failure.json").exists()


@pytest.mark.parametrize("mutate", [
    lambda r: r.refs.__setitem__(f"{BACKEND}:sha-{REV_NEW}", "sha256:" + "9" * 64),
    lambda r: r.labels[NEW_F].__setitem__(bfx.REVISION_LABEL, REV_OLD),
    lambda r: r.labels[NEW_B].pop(bfx.REVISION_LABEL),
])
def test_main_tag_revision_tag_and_frontend_must_agree(harness: Harness,
                                                       mutate: Callable[[FakeRegistry], None]) -> None:
    mutate(harness.registry)
    assert harness.run() == 1
    assert harness.host.calls == []


def test_ledger_write_failure_is_reported_not_swallowed(harness: Harness) -> None:
    harness.ledger.fail_append = {"deployed"}
    assert harness.run() == 1
    level, text = harness.notices[-1]
    assert level == "critical" and "LEDGER WRITE FAILED" in text


def test_unexpected_exception_alerts_instead_of_dying_silently(harness: Harness) -> None:
    def explode(reference: str) -> Any:
        raise RuntimeError("boom")

    harness.registry.inspect = explode  # type: ignore[method-assign]
    assert harness.run() == 1
    assert harness.notices[-1][0] == "critical"


def test_dry_run_reports_the_plan_and_changes_nothing(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    harness.host.current = ("h1",)
    harness.host.foreign = ["bfx-bot", "bfx-webapi", "bfx-frontend"]
    rows = len(harness.ledger.rows)
    assert harness.run(dry_run=True) == 0
    host = harness.host
    assert host.count(BACKUP) == 0 and host.count(_alembic("upgrade", "head")) == 0
    assert host.count(COMPOSE_UP) == 0 and host.stopped == [] and host.worktrees == {}
    assert len(harness.ledger.rows) == rows and harness.notices == []
    plan = json.loads(capsys.readouterr().out)
    assert plan["migrations_pending"] is True and plan["backup_before_migration"] is True
    assert plan["stop_bot_before_migration"] is True and plan["ci_run"] == CI_RUN
    assert plan["restore_test_before_deploy"] == "migration_pending"
    assert harness.host.count(RESTORE_TEST) == 0
    assert plan["change_class"] == "standard" and plan["rollback_target"] == REV_OLD
    assert plan["blockers"] == [f"foreign_container_holds_name:{n}"
                                for n in ("bfx-bot", "bfx-webapi", "bfx-frontend")]


# --------------------------------------------------------------------------- container check unit


def _target() -> Any:
    return bfx.Target(REV_NEW, NEW_B, NEW_F, f"{BACKEND}@{NEW_B}", f"{FRONTEND}@{NEW_F}")


@pytest.mark.parametrize("service", ["bot", "webapi", "frontend"])
def test_container_running_the_recorded_release_has_no_mismatch(service: str) -> None:
    target = _target()
    backend = service != "frontend"
    running = (target.backend_image if backend else target.frontend_image,
               NEW_B if backend else NEW_F, REV_NEW, "material", "dep-1")
    assert bfx.container_mismatches(inspect_record(service, running), service=service, target=target,
                                    klass="material", deployment_id="dep-1") == []


@pytest.mark.parametrize(("mutate", "mismatch"), [
    (lambda r: r.__setitem__("Name", "/other"), "name"),
    (lambda r: r["Config"].__setitem__("Labels", {}), "project"),
    (lambda r: r["Config"].__setitem__("Image", f"{FRONTEND}@{NEW_B}"), "image"),
    (lambda r: r["Config"]["Env"].__setitem__(0, "BFX_IMAGE_DIGEST=" + NEW_B), "identity_env"),
    (lambda r: r["Config"]["Env"].__setitem__(2, "BFX_CHANGE_CLASS=standard"), "identity_env"),
    (lambda r: r["Config"]["Env"].__setitem__(3, "BFX_DEPLOYMENT_ID=other"), "identity_env"),
])
def test_each_release_mismatch_is_reported(mutate: Callable[[dict[str, Any]], None], mismatch: str) -> None:
    target = _target()
    record = inspect_record("frontend", (target.frontend_image, NEW_F, REV_NEW, "material", "dep-1"))
    mutate(record)
    assert mismatch in bfx.container_mismatches(record, service="frontend", target=target,
                                                klass="material", deployment_id="dep-1")


# --------------------------------------------------------------------------- imagetools registry


def _imagetools(outputs: dict[str, tuple[int, str, str]]) -> tuple[Any, list[Any]]:
    seen: list[Any] = []

    def runner(argv: Sequence[str], *, timeout: float, input_text: str | None = None,
               env: Mapping[str, str] | None = None) -> Any:
        seen.append((list(argv), env))
        assert list(argv[:4]) == ["docker", "buildx", "imagetools", "inspect"]
        assert list(argv[5:]) == ["--format", bfx.IMAGETOOLS_FORMAT]
        code, out, err = outputs[argv[4]]
        return bfx.CommandResult(code, out, err)

    return runner, seen


def _index_output(digest: str, revision: str) -> str:
    labels = {bfx.REVISION_LABEL: revision, bfx.CI_RUN_LABEL: CI_RUN}
    return json.dumps({
        "manifest": {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest,
                     "manifests": []},
        "image": {"linux/amd64": {"config": {"Labels": {bfx.REVISION_LABEL: "x" * 40}}},
                  "linux/arm64": {"config": {"Labels": labels}}},
    })


def test_imagetools_reads_the_index_digest_and_the_arm64_labels() -> None:
    runner, seen = _imagetools({f"{BACKEND}:main": (0, _index_output(NEW_B, REV_NEW), "")})
    auth_env = {"DOCKER_CONFIG": "/tmp/x"}

    @__import__("contextlib").contextmanager
    def auth() -> Any:
        yield auth_env

    info = bfx.ImagetoolsRegistry(runner, auth).inspect(f"{BACKEND}:main")
    assert info.digest == NEW_B
    assert info.labels[bfx.REVISION_LABEL] == REV_NEW and info.labels[bfx.CI_RUN_LABEL] == CI_RUN
    assert seen[0][1] == auth_env


def test_imagetools_reads_a_single_platform_manifest() -> None:
    single = json.dumps({"manifest": {"digest": NEW_F},
                         "image": {"architecture": "arm64", "config": {"Labels": {bfx.REVISION_LABEL: REV_NEW}}}})
    runner, _ = _imagetools({f"{FRONTEND}@{NEW_F}": (0, single, "")})
    info = bfx.ImagetoolsRegistry(runner, lambda: __import__("contextlib").nullcontext({})).inspect(
        f"{FRONTEND}@{NEW_F}")
    assert (info.digest, info.labels[bfx.REVISION_LABEL]) == (NEW_F, REV_NEW)


@pytest.mark.parametrize(("output", "code"), [
    ((1, "", "ERROR: ghcr.io/x:sha-1: not found"), "registry_not_found"),
    ((1, "", "denied: permission"), "registry_inspect_failed"),
    ((0, "not json", ""), "registry_inspect_unparsable"),
    ((0, json.dumps({"manifest": {"digest": "sha256:short"}, "image": {"config": {}}}), ""),
     "registry_inspect_unparsable"),
    ((0, json.dumps({"manifest": {"digest": NEW_B}, "image": {"linux/amd64": {"config": {}}}}), ""),
     "registry_inspect_unparsable"),
])
def test_imagetools_failures_are_discovery_errors(output: tuple[int, str, str], code: str) -> None:
    runner, _ = _imagetools({f"{BACKEND}:main": output})
    registry = bfx.ImagetoolsRegistry(runner, lambda: __import__("contextlib").nullcontext({}))
    with pytest.raises(bfx.DiscoveryError, match=code):
        registry.inspect(f"{BACKEND}:main")


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
        attempt_id=OLD_ATTEMPT, started_at="2026-09-25T00:00:00+00:00", finished_at=None,
        source_revision=REV_NEW, backend_digest=NEW_B, frontend_digest=NEW_F,
        change_class="material", migrations_applied=True, outcome="started", detail=detail))
    assert ledger_id == 7
    assert seen["argv"][:6] == ["docker", "exec", "-i", "--user", "postgres", "bfx-postgres"]
    assert f"detail={detail}" in seen["argv"] and "finished_at=" in seen["argv"]
    assert detail not in seen["sql"] and ":'detail'" in seen["sql"]


def test_load_registry_credentials(tmp_path: Path) -> None:
    assert bfx.load_registry_credentials(tmp_path / "absent.env", lambda p: None) is None
    path = tmp_path / "ghcr.env"
    path.write_text("GHCR_USERNAME=will413028\nGHCR_TOKEN=ghp_x\n")
    assert bfx.load_registry_credentials(path, lambda p: None) == ("will413028", "ghp_x")
    path.write_text("GHCR_USERNAME=will413028\n")
    with pytest.raises(bfx.DeployError, match="incomplete"):
        bfx.load_registry_credentials(path, lambda p: None)


def test_secret_file_must_be_a_root_owned_0600_regular_file(tmp_path: Path) -> None:
    uid = os.getuid()
    secret = tmp_path / "bot.env"
    secret.write_text("A=1\n")
    secret.chmod(0o640)
    with pytest.raises(bfx.DeployError, match="mode_not_0600"):
        bfx.protected_secret_file(secret, owner_uid=uid)
    secret.chmod(0o600)
    with pytest.raises(bfx.DeployError, match="not_root_owned"):
        bfx.protected_secret_file(secret, owner_uid=uid + 1)
    link = tmp_path / "link.env"
    link.symlink_to(secret)
    with pytest.raises(bfx.DeployError, match="secret_file_missing"):
        bfx.protected_secret_file(link, owner_uid=uid)
    with pytest.raises(bfx.DeployError, match="secret_file_missing"):
        bfx.protected_secret_file(tmp_path / "absent.env", owner_uid=uid)
    bfx.protected_secret_file(secret, owner_uid=uid)  # no ancestor walk (design review #7)


def test_managed_units_file_names_only_units_that_exist() -> None:
    names = bfx.managed_units(ROOT / "deploy/vm/systemd/managed-units")
    assert all((ROOT / "deploy/vm/systemd" / name).is_file() for name in names)
    assert "bfx-restore-test@.service" in names and "bfx-deploy.service" in names
