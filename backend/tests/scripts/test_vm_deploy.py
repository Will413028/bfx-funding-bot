"""bfx-deploy decision flow against a simulated VM (no Docker, no network).

The simulated host answers the exact argv bfx-deploy issues (git through
runuser, docker pull/run/compose/inspect/exec/stop, backup.sh, systemctl, uv)
and keeps just enough state -- schema revision, running images, health by
digest, git worktrees -- to prove the ordering and fail-safe rules: the web API
and the bot are stopped before a migration and stay stopped if anything after
that fails (full stop, ADR 2026-10-15 D6), a
backup and a restore test with the target's DR scripts precede the migration,
the `started` row precedes any container change, rollback only when no
migration ran, and the target's tooling is installed only once it deployed.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

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
COMPOSE_TEXT = (ROOT / "deploy/vm/docker-compose.app.yml").read_text(encoding="utf-8")
LIVE_ENV_TEXT = (ROOT / "deploy/vm/live.env").read_text(encoding="utf-8")
# From 9d-5 the compose file has no frontend service (and CI builds no frontend image).
NO_FRONTEND_TEXT = COMPOSE_TEXT[:COMPOSE_TEXT.index("  frontend:\n")] \
    + COMPOSE_TEXT[COMPOSE_TEXT.index("networks:\n  bfx_default:\n    external"):]
TAILSCALE = "docker.io/tailscale/tailscale@sha256:" + "c" * 64
SERVE_PATH = "deploy/vm/tailscale/serve.json"
# The 9d-4 shape in miniature: a third-party sidecar and a file-backed config.
SIDECAR = f"""  tailscale:
    image: {TAILSCALE}
    pull_policy: never
    container_name: bfx-tailscale
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "-T", "3", "http://127.0.0.1:9002/healthz"]
      interval: 5s
    configs:
      - source: ts-serve
        target: /config/serve.json

"""
CONFIGS = "configs:\n  ts-serve:\n    file: ./tailscale/serve.json\n"
CONTAINER_NAMES = {"bot": "bfx-bot", "webapi": "bfx-webapi", "frontend": "bfx-frontend",
                   "tailscale": "bfx-tailscale"}
SERVICE_OF = {container: service for service, container in CONTAINER_NAMES.items()}


def with_sidecar(text: str, *, configs: bool = True) -> str:
    at = text.index("networks:\n  bfx_default:\n    external")
    return text[:at] + SIDECAR + text[at:] + (CONFIGS if configs else "")


# The 9d-5 shape: the web API runs in the sidecar's network namespace.
WEBAPI_NETWORKS = "    networks:\n      bfx_default: {}\n\n  frontend:"
JOINED_TEXT = with_sidecar(COMPOSE_TEXT, configs=False).replace(
    WEBAPI_NETWORKS, "    network_mode: service:tailscale\n\n  frontend:")
assert JOINED_TEXT.count("network_mode: service:tailscale") == 1


TOOLING = {
    str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
    for base in ("deploy/vm/ops", "deploy/vm/systemd")
    for path in sorted((ROOT / base).rglob("*"))
    if path.is_file() and "__pycache__" not in path.parts and ".venv" not in path.parts
}


# --------------------------------------------------------------------------- fakes


_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:\?|:-)([^}]*))?\}")


def render_compose(compose_file: Path, env: Mapping[str, str]) -> dict[str, Any] | None:
    """`docker compose config --no-env-resolution --format json`, for the part of Compose
    these files use (shape measured on Compose v5.1.4): variables interpolated, env_file
    and config/secret `file` paths made absolute against the compose file's directory,
    env file contents never read. None when a required variable is unset."""
    def interpolate(value: Any) -> Any:
        if isinstance(value, str):
            def substitute(match: re.Match[str]) -> str:
                name, operator, argument = match.group(1), match.group(2), match.group(3) or ""
                current = env.get(name, "")
                if operator == ":?" and not current:
                    raise LookupError(name)
                return argument if operator == ":-" and not current else current
            return _VARIABLE.sub(substitute, value)
        if isinstance(value, list):
            return [interpolate(item) for item in value]
        if isinstance(value, dict):
            return {key: interpolate(item) for key, item in value.items()}
        return value

    try:
        document = interpolate(yaml.safe_load(compose_file.read_text()))
    except LookupError:
        return None
    base = compose_file.parent
    for spec in document["services"].values():
        if spec.get("env_file"):
            spec["env_file"] = [
                {**({"path": entry} if isinstance(entry, str) else entry),
                 "path": os.path.normpath(base / (entry if isinstance(entry, str) else entry["path"]))}
                for entry in spec["env_file"]]
    for section in ("configs", "secrets"):
        for item in (document.get(section) or {}).values():
            if "file" in item:
                item["file"] = os.path.normpath(base / item["file"])
    return dict(document)


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
             attempt_id: str = OLD_ATTEMPT) -> None:
        self.rows.append(bfx.LedgerEntry(
            attempt_id=attempt_id,
            started_at="2026-09-24T00:00:00+00:00", finished_at="2026-09-24T00:01:00+00:00",
            source_revision=revision, backend_digest=backend, frontend_digest=frontend,
            migrations_applied=False, outcome=outcome, detail="seed"))

    def _row(self, index: int) -> Any:
        entry = self.rows[index]
        return bfx.LedgerRow(id=index + 1, attempt_id=entry.attempt_id,
                             source_revision=entry.source_revision,
                             backend_digest=entry.backend_digest,
                             frontend_digest=entry.frontend_digest,
                             outcome=entry.outcome,
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


def inspect_record(service: str, running: tuple[str, str, str, str],
                   meta: Mapping[str, str] | None = None, *, is_running: bool = True) -> dict[str, Any]:
    image, digest, revision, deployment_id = running
    meta = meta or {}
    # BFX_CHANGE_CLASS=retired: containers created before the follow-up release
    # still carry the transitional default; the identity check must ignore it.
    # A third-party container (empty digest here) carries no release identity.
    env = [f"BFX_IMAGE_DIGEST={digest}", f"BFX_SOURCE_REVISION={revision}",
           f"BFX_DEPLOYMENT_ID={deployment_id}", "BFX_CHANGE_CLASS=retired"] if digest else []
    env.append("PATH=/usr/bin")
    return {
        "Name": "/" + CONTAINER_NAMES[service], "Id": meta.get("Id", ""),
        "State": {"Running": is_running, "StartedAt": meta.get("StartedAt", "")},
        "HostConfig": {"NetworkMode": meta.get("NetworkMode", "bfx_default")},
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
    # What a failing restore test's drill leaves as its receipt (None: nothing written).
    restore_receipt: dict[str, Any] | None = None
    restore_evidence: Path | None = None
    start_ok: bool = True
    stop_ok_after_start: bool = True
    local_digests: dict[str, list[str]] = field(default_factory=dict)  # repository -> digests
    uv_ok: bool = True
    stop_ok: bool = True
    stop_fails: set[str] = field(default_factory=set)   # containers whose `docker stop` fails
    start_fails: set[str] = field(default_factory=set)  # containers whose `docker start` fails
    compose_fail: set[str] = field(default_factory=set)
    unhealthy: set[str] = field(default_factory=set)
    tampered: dict[str, Callable[[dict[str, Any]], None]] = field(default_factory=dict)
    foreign: list[str] = field(default_factory=list)
    running: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    stopped: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    parked: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)  # stopped
    worktrees: dict[str, str] = field(default_factory=dict)
    docker_configs: list[dict[str, Any]] = field(default_factory=list)
    pulls: list[tuple[str, bool]] = field(default_factory=list)  # (image, with a DOCKER_CONFIG)
    symlinks: set[tuple[str, str]] = field(default_factory=set)   # (revision, path) tracked as links
    # Per service: the container's Id, State.StartedAt and HostConfig.NetworkMode.
    meta: dict[str, dict[str, str]] = field(default_factory=dict)
    sequence: int = 0
    sidecar_health: str = "healthy"
    heads_by_digest: dict[str, tuple[str, ...]] = field(default_factory=dict)
    timeouts: set[tuple[str, ...]] = field(default_factory=set)  # argv prefixes that time out
    probes: list[str] = field(default_factory=list)              # URLs probed with docker exec

    def __post_init__(self) -> None:
        for revision in (REV_OLD, REV_NEW):
            self.files[(revision, bfx.COMPOSE_PATH)] = COMPOSE_TEXT
            self.files[(revision, bfx.LIVE_ENV_PATH)] = LIVE_ENV_TEXT
            for path, text in TOOLING.items():
                self.files[(revision, path)] = text
        self.diffs.setdefault((REV_OLD, REV_NEW), ["docs/runbooks/offsite-dr.md"])
        self.start_release(REV_OLD, OLD_B, OLD_F, OLD_ATTEMPT)

    def start_release(self, revision: str, backend: str, frontend: str, deployment_id: str) -> None:
        for service in ("bot", "webapi"):
            self.running[service] = (f"{BACKEND}@{backend}", backend, revision, deployment_id)
        self.running["frontend"] = (f"{FRONTEND}@{frontend}", frontend, revision, deployment_id)
        for service in ("bot", "webapi", "frontend"):
            self.create(service)

    def _tick(self) -> int:
        self.sequence += 1
        return self.sequence

    def create(self, service: str, network_mode: str = "bfx_default") -> None:
        """A new container: another Id, a later start."""
        tick = self._tick()
        self.meta[service] = {"Id": f"{tick:064x}", "StartedAt": self._time(tick),
                              "NetworkMode": network_mode}

    def restart_in_place(self, service: str) -> None:
        """The same container started again (its restart policy, `docker start`)."""
        self.meta[service]["StartedAt"] = self._time(self._tick())

    @staticmethod
    def _time(tick: int) -> str:
        # Docker's RFC 3339 with nanoseconds, trailing zeros trimmed.
        return f"2026-10-20T08:{tick // 60 % 60:02d}:{tick % 60:02d}.{tick * 10:d}Z"

    def run_joined(self) -> None:
        """The 9d-5 shape running: a sidecar and the web API in its namespace."""
        self.running["tailscale"] = (TAILSCALE, "", "", "")
        self.create("tailscale")
        self.create("webapi", "container:" + self.meta["tailscale"]["Id"])

    def index(self, predicate: Callable[[tuple[str, ...]], bool]) -> int:
        return next(i for i, call in enumerate(self.calls) if predicate(call))

    def count(self, predicate: Callable[[tuple[str, ...]], bool]) -> int:
        return sum(1 for call in self.calls if predicate(call))

    def __call__(self, argv: Sequence[str], *, timeout: float, input_text: str | None = None,
                 env: Mapping[str, str] | None = None) -> Any:
        call = tuple(argv)
        self.calls.append(call)
        self.envs.append(env)
        if any(call[:len(prefix)] == prefix for prefix in self.timeouts):
            raise bfx.CommandError(f"timeout:{bfx._describe(call)}", timed_out=True)
        ok = bfx.CommandResult(0, "", "")
        if call[:4] == ("runuser", "-u", "ubuntu", "--") and call[4] == "git":
            assert call[5] == "-C"
            return self._git(Path(call[6]), call[7:])
        if call[:4] == ("runuser", "-u", "ubuntu", "--") and call[4].endswith("backup.sh"):
            assert call[5:] == ("--type", "diff")
            return ok if self.backup_ok else bfx.CommandResult(2, "", "backup_evidence_unavailable")
        if call[:2] == ("systemctl", "start"):
            assert call[2].startswith("bfx-restore-test@") and call[2].endswith(".service")
            if self.restore_test_ok:
                return ok
            if self.restore_receipt is not None and self.restore_evidence is not None:
                self.restore_evidence.parent.mkdir(parents=True, exist_ok=True)
                self.restore_evidence.write_text(json.dumps(self.restore_receipt))
            return bfx.CommandResult(1, "", "Job failed")
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
            lines += [f"{CONTAINER_NAMES[s]}\tbfx-app" for s in self.running]
            return bfx.CommandResult(0, "\n".join(lines), "")
        if call[:2] == ("docker", "pull"):
            if env and "DOCKER_CONFIG" in env:
                config = Path(env["DOCKER_CONFIG"]) / "config.json"
                self.docker_configs.append(json.loads(config.read_text()))
            self.pulls.append((call[-1], bool(env and "DOCKER_CONFIG" in env)))
            return ok
        if call[:2] == ("docker", "run"):
            return self._alembic(call)
        if call[:2] == ("docker", "compose"):
            return self._compose(call, env)
        if call[:2] == ("docker", "inspect"):
            return self._inspect(call)
        if call[:2] == ("docker", "exec"):
            self.probes.append(call[-1])
            service = SERVICE_OF[call[2]]
            healthy = service in self.running and self.running[service][1] not in self.unhealthy
            return bfx.CommandResult(0 if healthy else 1, "", "")
        if call[:2] == ("docker", "stop"):
            container = call[-1]
            service = SERVICE_OF[container]
            if (not self.stop_ok or container in self.stop_fails
                    or (self.started and not self.stop_ok_after_start)):
                return bfx.CommandResult(1, "", "daemon unreachable")
            if service not in self.running:
                return bfx.CommandResult(1, "", f"Error response from daemon: No such container: {container}")
            self.parked[service] = self.running.pop(service)
            self.stopped.append(container)
            return ok
        if call[:2] == ("docker", "start"):
            assert len(call) == 3 and call[2] in {"bfx-bot", "bfx-webapi"}
            container = call[2]
            service = SERVICE_OF[container]
            if not self.start_ok or container in self.start_fails or service not in self.parked:
                return bfx.CommandResult(1, "", "Error response from daemon")
            self.running[service] = self.parked.pop(service)
            self.restart_in_place(service)
            self.started.append(container)
            return ok
        if call[:3] == ("docker", "image", "ls"):
            return bfx.CommandResult(0, "\n".join(self.local_digests.get(call[-1], [])), "")
        if call[:2] in {("docker", "tag"), ("docker", "rm")} or call[:3] == ("docker", "image", "rm"):
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
        if args[0] == "ls-tree" and args[2] == "--":
            revision, path = args[1], args[3]
            if (revision, path) not in self.files:
                return bfx.CommandResult(0, "", "")
            mode = "120000" if (revision, path) in self.symlinks else "100644"
            return bfx.CommandResult(0, f"{mode} blob {'0' * 40}\t{path}\n", "")
        if args[:4] == ("ls-tree", "-r", "-z", "--name-only"):
            revision, prefixes = args[4], args[6:]
            listed = [path for (rev, path) in self.files if rev == revision
                      and any(path.startswith(prefix + "/") for prefix in prefixes)]
            return bfx.CommandResult(0, "".join(p + "\0" for p in sorted(listed)), "")
        if args[:4] == ("worktree", "add", "--detach", "--force"):
            path, revision = Path(args[4]), args[5]
            assert revision in self.commits
            # git inherits bfx-deploy.service's UMask=0077.
            scripts = path / "deploy/vm/pgbackrest"
            scripts.mkdir(mode=0o700, parents=True)
            for directory in (path, path / "deploy", path / "deploy/vm", scripts):
                directory.chmod(0o700)
            (scripts / "pgbackrest.conf").write_text("[bfx]\n")
            (scripts / "pgbackrest.conf").chmod(0o600)
            (scripts / "backup.sh").write_text("#!/bin/sh\n")
            (scripts / "backup.sh").chmod(0o700)
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
            return bfx.CommandResult(0, "".join(f"{h} (head)\n" for h in self._heads(call)), "")
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

    def _heads(self, call: tuple[str, ...]) -> tuple[str, ...]:
        image = call[call.index("/app/.venv/bin/alembic") - 1]
        return self.heads_by_digest.get(image.rpartition("@")[2], self.heads)

    def _compose(self, call: tuple[str, ...], env: Mapping[str, str] | None) -> Any:
        assert call[:5] == ("docker", "compose", "-p", "bfx-app", "-f")
        compose_file = Path(call[5])
        revision = compose_file.parent.name
        assert compose_file.read_text() == self.files[(revision, bfx.COMPOSE_PATH)]
        assert (compose_file.parent / "live.env").read_text() == LIVE_ENV_TEXT
        if call[6:] == ("config", "--no-interpolate", "--services"):
            services = yaml.safe_load(compose_file.read_text())["services"]
            return bfx.CommandResult(0, "".join(f"{name}\n" for name in services), "")
        assert env is not None
        rendered = render_compose(compose_file, env)
        if call[6:] == ("config", "--no-env-resolution", "--format", "json"):
            if rendered is None:
                return bfx.CommandResult(1, "", "required variable is missing a value")
            return bfx.CommandResult(0, json.dumps(rendered), "")
        assert call[6] == "up" and rendered is not None
        options = tuple(part for part in call[7:] if part.startswith("--"))
        services = call[7 + len(options):]
        if services == ("webapi",):        # netns repair: the web API alone, nothing removed
            assert options == ("--detach", "--no-deps", "--force-recreate")
        else:
            assert options[:3] == ("--detach", "--no-deps", "--remove-orphans")
            assert options[3:] in {(), ("--force-recreate",)}
            assert services == tuple(sorted(rendered["services"]))
        if env["BFX_BACKEND_DIGEST"] in self.compose_fail:
            return bfx.CommandResult(1, "", "compose failed")
        assert "BFX_CHANGE_CLASS" not in env  # retired; compose supplies a transitional default
        if "--remove-orphans" in options:
            for orphan in set(self.running) - set(services):
                del self.running[orphan]
        identity = (env["BFX_SOURCE_REVISION"], env["BFX_DEPLOYMENT_ID"])
        # Compose creates a namespace's provider before the services that join it.
        joins = {name: str(rendered["services"][name].get("network_mode", "")).removeprefix("service:")
                 for name in services}
        for service in sorted(services, key=lambda name: name in joins and joins[name] in rendered["services"]):
            joined = joins[service] if joins[service] in rendered["services"] else None
            self.create(service, "container:" + self.meta[joined]["Id"] if joined else "bfx_default")
            image = rendered["services"][service]["image"]
            if image == TAILSCALE:
                self.running[service] = (image, "", "", "")
            else:
                digest = image.rpartition("@")[2]
                assert digest in {env["BFX_BACKEND_DIGEST"], env.get("BFX_FRONTEND_DIGEST")}
                self.running[service] = (image, digest, *identity)
        return bfx.CommandResult(0, "", "")

    def _inspect(self, call: tuple[str, ...]) -> Any:
        names = [part for part in call[2:] if part.startswith("bfx-")]
        if "--format" in call and call[call.index("--format") + 1] == bfx.HEALTH_FORMAT:
            assert names == ["bfx-tailscale"]
            return bfx.CommandResult(0, self.sidecar_health + "\n" if "tailscale" in self.running else "", "")
        if "--format" in call:
            lines = [f"/{name} 0 true" for name in names if SERVICE_OF[name] in self.running]
            return bfx.CommandResult(0, "\n".join(lines), "")
        records, missing = [], False
        for name in names:
            service = SERVICE_OF[name]
            if service not in self.running and service not in self.parked:
                missing = True                  # docker prints the others and exits 1
                continue
            is_running = service in self.running
            running = self.running[service] if is_running else self.parked[service]
            record = inspect_record(service, running, self.meta.get(service), is_running=is_running)
            digest = running[1]
            if digest in self.tampered:
                self.tampered[digest](record)
            records.append(record)
        return bfx.CommandResult(1 if missing else 0, json.dumps(records), "no such container" if missing else "")


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
        uv="uv", backup_user="ubuntu", restore_evidence=tmp_path / "dr-evidence/restore-ledger.json",
    )
    ledger = FakeLedger()
    ledger.seed(REV_OLD, OLD_B, OLD_F, "deployed")
    host = FakeHost(mirror=mirror, dr_root=settings.dr_root,
                    restore_evidence=settings.restore_evidence)
    return Harness(host=host, registry=FakeRegistry(),
                   ledger=ledger, clock=FakeClock(), notices=[], settings=settings)


def _is(prefix: tuple[str, ...]) -> Callable[[tuple[str, ...]], bool]:
    return lambda call: call[:len(prefix)] == prefix


def _alembic(*args: str) -> Callable[[tuple[str, ...]], bool]:
    return lambda call: call[:2] == ("docker", "run") and call[-len(args):] == args


BACKUP = lambda call: len(call) > 4 and call[4].endswith("backup.sh")  # noqa: E731
RESTORE_TEST = _is(("systemctl", "start"))


def _compose(verb: str) -> Callable[[tuple[str, ...]], bool]:
    return lambda call: call[:2] == ("docker", "compose") and call[6:7] == (verb,)


COMPOSE_UP = _compose("up")
STOP_BOT = _is(("docker", "stop", "--time", "30", "bfx-bot"))
STOP_WEBAPI = _is(("docker", "stop", "--time", "30", "bfx-webapi"))
STOP_ANY = _is(("docker", "stop"))
PULL = _is(("docker", "pull"))
UV_SYNC = _is(("uv", "sync"))


NETNS_INSPECT = ("docker", "inspect", "--type", "container", "bfx-tailscale", "bfx-webapi")


def _started(harness: Harness) -> list[Any]:
    return [row for row in harness.ledger.rows if row.outcome == "started"]


# --------------------------------------------------------------------------- no-op


def test_nothing_happens_when_main_already_runs(harness: Harness) -> None:
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, "deployed", attempt_id="n")
    rows = len(harness.ledger.rows)
    assert harness.run() == 0
    assert harness.host.calls == [NETNS_INSPECT]       # no sidecar yet: nothing else
    assert (harness.notices, len(harness.ledger.rows)) == ([], rows)


@pytest.mark.parametrize("outcome", ["rolled_back", "failed", "started"])
def test_a_digest_already_attempted_is_not_retried_every_tick(harness: Harness, outcome: str) -> None:
    # `started` without a terminal row: the attempt was interrupted (crash, kill).
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, outcome, attempt_id="n")
    assert harness.run() == 0
    assert harness.host.calls == ([NETNS_INSPECT] if outcome == "rolled_back" else [])
    assert harness.notices == []
    assert harness.run(retry=True) == 0
    assert harness.ledger.last.outcome == "deployed"


def test_second_run_is_skipped_while_the_lock_is_held(harness: Harness) -> None:
    harness.settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with harness.settings.lock_file.open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        assert harness.run() == 0
    assert harness.host.calls == []


# --------------------------------------------------------------------------- success


def test_release_without_migration_deploys_by_digest(harness: Harness) -> None:
    assert harness.run() == 0
    host = harness.host
    assert host.count(BACKUP) == 0 and host.count(STOP_ANY) == 0
    assert host.count(_alembic("upgrade", "head")) == 0
    assert host.index(PULL) < host.index(_alembic("current")) < host.index(COMPOSE_UP)
    env = host.envs[host.index(COMPOSE_UP)]
    assert env is not None
    assert env["BFX_BACKEND_IMAGE"] == f"{BACKEND}@{NEW_B}"
    assert env["BFX_FRONTEND_IMAGE"] == f"{FRONTEND}@{NEW_F}"
    assert env["BFX_SOURCE_REVISION"] == REV_NEW and "BFX_CHANGE_CLASS" not in env
    # Compose reads the env files bfx-deploy validated, not a separate default.
    assert env["BFX_RUNTIME_DIR"] == str(harness.settings.runtime_dir)
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("deployed", False)
    assert "class=" not in row.detail and "class=" not in harness.notices[-1][1]
    assert (row.backend_digest, row.frontend_digest, row.source_revision) == (NEW_B, NEW_F, REV_NEW)
    assert harness.notices[-1][0] == "info"
    assert host.stopped == []
    assert host.count(_is(("docker", "tag", f"{BACKEND}@{NEW_B}", "bfx-bot:local"))) == 1


def test_started_row_precedes_the_containers_and_names_their_deployment_id(harness: Harness) -> None:
    ledger_rows_at_compose: list[int] = []
    original = harness.host._compose

    def compose(call: tuple[str, ...], env: Mapping[str, str] | None) -> Any:
        if COMPOSE_UP(call):
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
            started.started_at) == (
        finished.source_revision, finished.backend_digest, finished.frontend_digest,
        finished.started_at)
    env = harness.host.envs[harness.host.index(COMPOSE_UP)]
    assert env is not None and env["BFX_DEPLOYMENT_ID"] == started.attempt_id
    assert harness.host.running["bot"][3] == started.attempt_id
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


def test_pending_migration_stops_the_webapi_and_the_bot_then_backs_up_restore_tests_and_migrates(
    harness: Harness,
) -> None:
    """Full stop (ADR 2026-10-15 D6): no web API runs while the schema changes."""
    harness.host.current = ("h1",)
    assert harness.run() == 0
    host = harness.host
    assert (host.index(STOP_WEBAPI) < host.index(STOP_BOT) < host.index(BACKUP)
            < host.index(RESTORE_TEST) < host.index(_alembic("upgrade", "head"))
            < host.index(COMPOSE_UP))
    assert host.stopped == ["bfx-webapi", "bfx-bot"] and host.started == []
    # The target release's DR scripts, from a clean checkout of <rev>.
    checkout = harness.settings.dr_root / REV_NEW
    assert host.calls[host.index(BACKUP)][4] == str(checkout / "deploy/vm/pgbackrest/backup.sh")
    assert host.calls[host.index(RESTORE_TEST)] == ("systemctl", "start", f"bfx-restore-test@{REV_NEW}.service")
    assert host.worktrees[str(checkout)] == REV_NEW
    started = _started(harness)[-1]
    assert started.migrations_applied is True
    assert harness.ledger.last.migrations_applied is True
    assert harness.ledger.last.outcome == "deployed"
    assert host.running["bot"][1] == host.running["webapi"][1] == NEW_B


def test_change_classes_are_gone_from_the_command_line() -> None:
    with pytest.raises(SystemExit):
        bfx._parser().parse_args(["--force-material"])
    assert "force_material" not in bfx.Settings.__dataclass_fields__


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
    # The weekly job ships with the tooling: its steps, runner and unit follow each deploy,
    # its timer (an operator's schedule, possibly masked) is never overwritten.
    for path in ("ops/docker-compose.weekly-report.yml", "ops/bfx_weekly_report.py"):
        assert (release / path).read_text() == TOOLING[f"deploy/vm/{path}"]
    assert (harness.settings.unit_dir / "bfx-weekly-report.service").read_text() == \
        TOOLING["deploy/vm/systemd/bfx-weekly-report.service"]
    assert not (harness.settings.unit_dir / "bfx-weekly-report.timer").exists()
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


def test_bot_that_cannot_be_stopped_blocks_the_migration_and_starts_the_webapi_again(
    harness: Harness,
) -> None:
    host = harness.host
    host.current = ("h1",)
    host.stop_fails = {"bfx-bot"}
    assert harness.run() == 1
    assert host.count(BACKUP) == 0 and host.count(_alembic("upgrade", "head")) == 0
    # Nothing migrated: the web API stopped for it serves the running release again.
    assert host.stopped == ["bfx-webapi"] and host.started == ["bfx-webapi"]
    assert host.running["webapi"] == (f"{BACKEND}@{OLD_B}", OLD_B, REV_OLD, OLD_ATTEMPT)
    assert harness.ledger.last.detail == (
        "bot_stop_failed; nothing migrated; running release untouched; webapi restarted")


def test_webapi_that_cannot_be_stopped_blocks_the_migration_with_the_bot_running(
    harness: Harness,
) -> None:
    host = harness.host
    host.current = ("h1",)
    host.stop_fails = {"bfx-webapi"}
    assert harness.run() == 1
    assert host.count(STOP_BOT) == 0 and host.count(BACKUP) == 0
    assert host.count(_alembic("upgrade", "head")) == 0 and host.count(COMPOSE_UP) == 0
    assert host.stopped == [] and host.running["bot"][1] == OLD_B
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", False)
    assert row.detail == "webapi_stop_failed; nothing migrated; running release untouched"


@pytest.mark.parametrize("service", ["bot", "webapi"])
def test_absent_container_does_not_block_a_first_migration(harness: Harness, service: str) -> None:
    harness.host.current = ("h1",)
    del harness.host.running[service]
    assert harness.run() == 0
    assert harness.ledger.last.outcome == "deployed"


def test_started_row_that_cannot_be_written_after_a_migration_keeps_both_stopped(
    harness: Harness,
) -> None:
    host = harness.host
    host.current = ("h1",)
    harness.ledger.fail_append = {"started"}
    assert harness.run() == 1
    assert host.count(COMPOSE_UP) == 0 and not {"bot", "webapi"} & set(host.running)
    assert harness.ledger.last.detail.endswith("; bot and webapi stay stopped")


@pytest.mark.parametrize(("setup", "code", "migrated"), [
    ("backup", "backup_failed", False),
    ("restore", "restore_test_failed(migration_pending)", False),
    ("fail_unchanged", "migration_failed", False),
    ("fail_partial", "migration_partial_or_unverified", True),
])
def test_any_failure_after_the_stop_keeps_the_bot_and_the_webapi_stopped(
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
    assert host.stopped == ["bfx-webapi", "bfx-bot"] and host.started == []
    assert not {"bot", "webapi"} & set(host.running)   # no old web API on a changed schema
    assert host.count(COMPOSE_UP) == 0 and _started(harness) == []
    if setup in {"backup", "restore"}:
        assert host.count(_alembic("upgrade", "head")) == 0
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", migrated)
    assert code in row.detail and "bot and webapi stopped until a release deploys" in row.detail
    assert harness.notices[-1][0] == "critical"


def _receipt(code: str = "migration_rehearsal_failed", *, observed_at_ms: int = 1_790_000_000_001,
             **changes: Any) -> dict[str, Any]:
    """A failed restore test's receipt, as restore_drill.py writes it (after FakeClock's now)."""
    return {"schema_version": 1, "measured": False, "kind": "restore_ledger",
            "observed_at_ms": observed_at_ms, "error_code": code, "source_revision": REV_NEW,
            **changes}


def test_a_failed_migration_rehearsal_starts_the_running_bot_and_webapi_again(
    harness: Harness,
) -> None:
    """D7' (Will 2026-10-08): the backup restored and production's schema is untouched; only the
    target's migrations failed on the isolated copy, so the release is refused and the bot and
    the web API that were stopped for it run on (each health-probed like a deploy)."""
    host = harness.host
    host.current = ("h1",)
    host.restore_test_ok = False
    host.restore_receipt = _receipt()
    assert harness.run() == 1
    assert host.stopped == ["bfx-webapi", "bfx-bot"] and host.started == ["bfx-bot", "bfx-webapi"]
    for service in ("bot", "webapi"):
        assert host.running[service] == (f"{BACKEND}@{OLD_B}", OLD_B, REV_OLD, OLD_ATTEMPT)
    assert host.count(_is(("docker", "exec", "bfx-webapi"))) >= 2  # probed, then after settle
    assert host.count(_alembic("upgrade", "head")) == 0
    assert host.count(COMPOSE_UP) == 0 and _started(harness) == []
    assert host.index(BACKUP) < host.index(RESTORE_TEST) < host.index(_is(("docker", "start")))
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied, row.backend_digest) == ("failed", False, NEW_B)
    assert row.detail == ("restore_test_failed(migration_pending):restore_test_unit_failed:"
            "migration_rehearsal_failed; schema unchanged; bot restarted on the running release; "
            "webapi restarted on the running release")
    assert harness.notices[-1][0] == "critical"
    # Refused like any failed digest: not retried every tick.
    calls = len(host.calls)
    assert harness.run() == 0 and len(host.calls) == calls


@pytest.mark.parametrize("receipt", [
    pytest.param(None, id="no-receipt"),
    pytest.param(_receipt("ledger_digest_mismatch"), id="restore-failed"),
    pytest.param(_receipt("restore_command_failed"), id="old-drill-without-rehearsal"),
    pytest.param(_receipt(observed_at_ms=1_789_999_999_999), id="older-than-this-restore-test"),
    pytest.param(_receipt(measured=True), id="measured"),
    pytest.param(_receipt(kind="backup"), id="other-kind"),
    pytest.param(_receipt(source_revision=REV_OLD), id="monthly-run-of-the-deployed-release"),
    pytest.param({k: v for k, v in _receipt().items() if k != "source_revision"},
                 id="no-revision"),
    pytest.param(_receipt(observed_at_ms="1790000000001"), id="untyped-time"),
])
def test_any_other_restore_test_failure_keeps_the_bot_and_webapi_stopped(
    harness: Harness, receipt: dict[str, Any] | None,
) -> None:
    host = harness.host
    host.current = ("h1",)
    host.restore_test_ok = False
    host.restore_receipt = receipt
    assert harness.run() == 1
    assert host.stopped == ["bfx-webapi", "bfx-bot"] and host.started == []
    assert not {"bot", "webapi"} & set(host.running)
    row = harness.ledger.last
    assert "restore_test_failed(migration_pending):restore_test_unit_failed; schema unchanged; " \
        "bot and webapi stopped until a release deploys" in row.detail


@pytest.mark.parametrize("planted", ["symlink", "directory", "garbage", "oversized"])
def test_an_unusable_receipt_keeps_the_bot_stopped(harness: Harness, planted: str) -> None:
    host, path = harness.host, harness.settings.restore_evidence
    host.current = ("h1",)
    host.restore_test_ok = False
    path.parent.mkdir(parents=True)
    if planted == "symlink":  # the unit's user owns the directory; root never follows a link
        real = path.with_name("real.json")
        real.write_text(json.dumps(_receipt()))
        path.symlink_to(real)
    elif planted == "directory":
        path.mkdir()
    elif planted == "garbage":
        path.write_text("{not json")
    else:
        path.write_text(json.dumps(_receipt(padding="x" * bfx._MAX_EVIDENCE_BYTES)))
    assert harness.run() == 1
    assert host.started == [] and not {"bot", "webapi"} & set(host.running)


def test_a_failed_backup_keeps_the_bot_stopped_even_beside_a_rehearsal_receipt(
    harness: Harness,
) -> None:
    host, path = harness.host, harness.settings.restore_evidence
    host.current = ("h1",)
    host.backup_ok = False
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_receipt()))
    assert harness.run() == 1
    assert host.started == [] and not {"bot", "webapi"} & set(host.running)
    assert host.count(RESTORE_TEST) == 0
    assert "backup_failed" in harness.ledger.last.detail


@pytest.mark.parametrize(("setup", "detail"), [
    ("start_fails", "bot_restart_failed:start_failed; bot stopped until a release deploys"),
    ("unhealthy", "bot_restart_failed:unhealthy; bot stopped until a release deploys"),
    ("unhealthy_and_unstoppable", "bot_restart_failed:unhealthy; BOT_STOP_FAILED, bot may be running"),
])
def test_a_bot_that_does_not_come_back_is_left_stopped(
    harness: Harness, setup: str, detail: str,
) -> None:
    host = harness.host
    host.current = ("h1",)
    host.restore_test_ok = False
    host.restore_receipt = _receipt()
    if setup == "start_fails":
        host.start_ok = False
    else:
        host.unhealthy = {OLD_B}
    if setup == "unhealthy_and_unstoppable":
        host.stop_ok_after_start = False
    assert harness.run() == 1
    assert detail in harness.ledger.last.detail
    if setup == "unhealthy":
        assert host.stopped == ["bfx-webapi", "bfx-bot", "bfx-bot", "bfx-webapi"]
        assert not {"bot", "webapi"} & set(host.running)
        assert "webapi_restart_failed:unhealthy; webapi stopped until a release deploys" \
            in harness.ledger.last.detail
    if setup == "start_fails":
        assert not {"bot", "webapi"} & set(host.running)


def test_a_webapi_that_does_not_come_back_leaves_the_restarted_bot_running(
    harness: Harness,
) -> None:
    host = harness.host
    host.current = ("h1",)
    host.restore_test_ok = False
    host.restore_receipt = _receipt()
    host.start_fails = {"bfx-webapi"}
    assert harness.run() == 1
    assert host.running["bot"][1] == OLD_B and "webapi" not in host.running
    assert harness.ledger.last.detail.endswith(
        "schema unchanged; bot restarted on the running release; "
        "webapi_restart_failed:start_failed; webapi stopped until a release deploys")


def test_a_restore_test_first_drops_other_digests_so_the_candidate_is_unambiguous(
    harness: Harness,
) -> None:
    """The drill finds its candidate by revision label; a stale digest of the same revision (a
    CI re-run after a failed attempt) would make it ambiguous."""
    host = harness.host
    host.current = ("h1",)
    stale = "sha256:" + "9" * 64
    host.local_digests = {BACKEND: [OLD_B, NEW_B, stale], FRONTEND: [OLD_F, NEW_F]}
    assert harness.run() == 0
    removed = host.index(_is(("docker", "image", "rm", f"{BACKEND}@{stale}")))
    assert removed < host.index(RESTORE_TEST)
    assert not any(call[:3] == ("docker", "image", "rm") and call[-1].endswith(digest)
                   for call in host.calls[:host.index(RESTORE_TEST)]
                   for digest in (OLD_B, NEW_B, OLD_F, NEW_F))


def test_a_failed_rehearsal_without_a_migration_leaves_the_running_release_alone(
    harness: Harness,
) -> None:
    """A DR-path release without a migration never stopped the bot: nothing to start."""
    host = harness.host
    harness.host.diffs[(REV_OLD, REV_NEW)] = ["deploy/vm/pgbackrest/restore_drill.py"]
    host.restore_test_ok = False
    host.restore_receipt = _receipt()
    assert harness.run() == 1
    assert host.stopped == [] and host.started == [] and host.count(COMPOSE_UP) == 0
    assert "nothing deployed; running release untouched" in harness.ledger.last.detail


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
    _assert_readable_by_restore_container(harness.settings.dr_root / REV_NEW)


def _assert_readable_by_restore_container(checkout: Path) -> None:
    # The isolated restore's postgres (uid 70) reads pgbackrest.conf through a
    # bind mount; a 0700 tree fails it with restore_permissions_invalid.
    scripts = checkout / "deploy/vm/pgbackrest"
    for directory in (checkout, checkout / "deploy", checkout / "deploy/vm", scripts):
        assert oct(directory.stat().st_mode & 0o777) == "0o755", directory
    assert oct((scripts / "pgbackrest.conf").stat().st_mode & 0o777) == "0o644"
    assert oct((scripts / "backup.sh").stat().st_mode & 0o777) == "0o755"


def test_new_checkout_is_readable_despite_the_service_umask(harness: Harness) -> None:
    harness.host.current = ("h1",)
    assert harness.run() == 0
    _assert_readable_by_restore_container(harness.settings.dr_root / REV_NEW)


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


@pytest.mark.parametrize("mutate", [
    lambda h: h.host.diffs.pop((REV_OLD, REV_NEW)),               # git diff fails
    lambda h: h.host.commits.discard(REV_OLD),                     # previous revision unknown
    lambda h: (h.ledger.rows.clear(), setattr(h.ledger, "exists", False)),  # first deployment
])
def test_unreadable_diff_runs_the_restore_test(harness: Harness,
                                               mutate: Callable[[Harness], None]) -> None:
    mutate(harness)
    assert harness.run() == 0
    assert harness.host.count(RESTORE_TEST) == 1
    assert harness.host.stopped == []   # no migration: the bot keeps running
    assert harness.ledger.last.outcome == "deployed"


@pytest.mark.parametrize(("pattern", "path", "expected"), [
    ("deploy/vm/pgbackrest/**", "deploy/vm/pgbackrest/pgbackrest.conf", True),
    ("deploy/vm/pgbackrest/**", "deploy/vm/pgbackrest/a/b/c.sh", True),
    ("deploy/vm/pgbackrest/**", "deploy/vm/pgbackrestx/a.sh", False),
    ("deploy/vm/pgbackrest/**", "deploy/vm/pgbackrest", False),
    ("docker-compose.bot.yml", "docker-compose.bot.yml", True),
    ("docker-compose.bot.yml", "deploy/docker-compose.bot.yml", False),  # anchored at the root
    ("docker-compose.*.yml", "docker-compose.dr.yml", True),
    ("docker-compose.*.yml", "deploy/docker-compose.dr.yml", False),
    ("backend/scripts/*.py", "backend/scripts/x.py", True),
    ("backend/scripts/*.py", "backend/scripts/sub/x.py", False),   # `*` stays in one segment
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("**/*.md", "README.md", True),
    ("**/*.md", "a/b/c.md", True),                                 # `**/` spans directories
    ("**/*.md", "a/b/c.mdx", False),
    ("a/**/z", "a/z", True),
    ("a/**/z", "a/b/c/z", True),
])
def test_path_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert bfx.path_matches([pattern], path) is expected


def test_path_glob_without_patterns_matches_nothing() -> None:
    assert bfx.path_matches([], "docker-compose.bot.yml") is False


@pytest.mark.parametrize(("path", "expected"), [
    ("deploy/vm/pgbackrest/backup.sh", True),
    ("deploy/vm/postgres/initdb/01.sql", True),
    ("docker-compose.bot.yml", True),
    ("docker-compose.dr.yml", True),
    ("deploy/vm/docker-compose.app.yml", False),
    ("deploy/vm/ops/bfx_deploy.py", False),
    ("examples/deploy/vm/pgbackrest/x.md", False),
])
def test_dr_trigger_patterns(path: str, expected: bool) -> None:
    assert bfx.path_matches(bfx.DR_TRIGGER_PATTERNS, path) is expected


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
    # The previous release comes back under its own deployment id.
    assert rollback_env["BFX_DEPLOYMENT_ID"] == OLD_ATTEMPT and "BFX_CHANGE_CLASS" not in rollback_env
    assert Path(host.calls[ups[1]][5]).parent.name == REV_OLD
    assert host.running["bot"][1] == OLD_B and host.stopped == []
    row = harness.ledger.last
    assert (row.outcome, row.backend_digest) == ("rolled_back", NEW_B)
    assert harness.ledger.attempt(row.attempt_id) == ["started", "rolled_back"]
    assert f"rolled_back_to={REV_OLD}" in row.detail
    assert harness.notices[-1][0] == "warning"
    assert host.count(_is(("docker", "tag"))) == 0


def test_unhealthy_release_after_migration_stops_the_bot_and_webapi_without_rollback(
    harness: Harness,
) -> None:
    harness.host.current = ("h1",)
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    host = harness.host
    assert host.count(COMPOSE_UP) == 1
    # Before the migration, and after the failed start: the web API stays down with the bot.
    assert host.stopped == ["bfx-webapi", "bfx-bot", "bfx-bot", "bfx-webapi"]
    assert not {"bot", "webapi"} & set(host.running)
    row = harness.ledger.last
    assert (row.outcome, row.migrations_applied) == ("failed", True)
    assert "no automatic rollback; bot stopped; webapi stopped" in row.detail
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


@pytest.mark.parametrize("setup", [
    "deployed", "dr_paths", "rolled_back", "rollback_failed", "first_deployment", "recreate_failed",
])
def test_a_deploy_without_a_migration_never_stops_the_webapi(harness: Harness, setup: str) -> None:
    """Full stop is for migrations only: every other path keeps its pre-D6 behaviour."""
    host = harness.host
    if setup == "dr_paths":
        host.diffs[(REV_OLD, REV_NEW)] = ["deploy/vm/pgbackrest/pgbackrest.conf"]
    elif setup == "rolled_back":
        host.unhealthy = {NEW_B}
    elif setup == "rollback_failed":
        host.unhealthy = {NEW_B, OLD_B}
    elif setup == "first_deployment":
        harness.ledger.rows.clear()
        host.unhealthy = {NEW_F}
    elif setup == "recreate_failed":
        host.unhealthy = {OLD_B}
    harness.run(recreate=setup == "recreate_failed")
    assert host.count(STOP_WEBAPI) == 0 and host.count(_is(("docker", "start"))) == 0
    assert "webapi" in host.running


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
    assert harness.host.count(STOP_ANY) == 0
    assert len(harness.ledger.rows) == rows


# --------------------------------------------------------------------------- the release's compose file


def _release(harness: Harness, revision: str = REV_NEW) -> Path:
    return harness.settings.state_dir / "releases" / revision


def _without_frontend(harness: Harness, revision: str = REV_NEW) -> None:
    """A 9d-5-style release: no frontend service, and CI built no frontend image."""
    harness.host.files[(revision, bfx.COMPOSE_PATH)] = NO_FRONTEND_TEXT
    harness.registry.refs.pop(f"{FRONTEND}:sha-{revision}")


def test_a_release_without_a_frontend_deploys_and_removes_the_old_frontend(harness: Harness) -> None:
    _without_frontend(harness)
    # Its env file is not the release's any more: an unreadable one blocks nothing.
    (harness.settings.runtime_dir / "frontend.env").write_text("BROKEN='quoted'\n")
    assert harness.run() == 0
    host = harness.host
    up = host.calls[host.index(COMPOSE_UP)]
    assert up[-2:] == ("bot", "webapi") and "--remove-orphans" in up
    assert "frontend" not in host.running          # the orphan went with --remove-orphans
    env = host.envs[host.index(COMPOSE_UP)]
    assert env is not None and "BFX_FRONTEND_IMAGE" not in env
    row = harness.ledger.last
    assert (row.outcome, row.frontend_digest) == ("deployed", None)
    assert [s.frontend_digest for s in _started(harness)] == [None]
    assert all(image != f"{FRONTEND}@{NEW_F}" for image, _ in host.pulls)


def test_a_release_with_a_frontend_still_deploys_all_three(harness: Harness) -> None:
    assert harness.run() == 0
    host = harness.host
    assert host.calls[host.index(COMPOSE_UP)][-3:] == ("bot", "frontend", "webapi")
    assert harness.ledger.last.frontend_digest == NEW_F


def test_rollback_to_a_release_with_a_frontend_brings_it_back(harness: Harness) -> None:
    _without_frontend(harness)
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    assert harness.ledger.last.outcome == "rolled_back"
    assert harness.host.running["frontend"] == (f"{FRONTEND}@{OLD_F}", OLD_F, REV_OLD, OLD_ATTEMPT)


def test_a_frontend_image_of_a_release_without_a_frontend_is_not_recorded(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = NO_FRONTEND_TEXT
    assert harness.run() == 0
    assert harness.ledger.last.frontend_digest is None
    assert all(image != f"{FRONTEND}@{NEW_F}" for image, _ in harness.host.pulls)


def test_env_files_are_the_ones_the_release_compose_file_names(harness: Harness) -> None:
    text = with_sidecar(COMPOSE_TEXT, configs=False).replace(
        "    container_name: bfx-tailscale\n",
        "    container_name: bfx-tailscale\n    env_file:\n"
        "      - ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/tailscale.env\n")
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = text
    (harness.settings.runtime_dir / "tailscale.env").write_text("TS_HOSTNAME=$host\n")
    assert harness.run() == 1
    assert harness.ledger.last.detail == ("precheck:env_file_value_ambiguous_for_compose:"
                                          "tailscale.env:TS_HOSTNAME; running release untouched")
    checked: list[Path] = []
    deployer = harness.deployer(retry=True)
    deployer._secret_check = checked.append
    (harness.settings.runtime_dir / "tailscale.env").write_text("TS_HOSTNAME=bfx-ops\n")
    assert deployer.run() == 0
    runtime = harness.settings.runtime_dir
    assert sorted(p.name for p in checked) == ["bot.env", "frontend.env", "migrate.env",
                                               "tailscale.env", "webapi.env"]
    assert all(p.parent == runtime for p in checked)


@pytest.mark.parametrize(("old", "new", "code"), [
    ("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env", "/etc/webapi.env",
     "compose_env_file_outside_runtime:webapi"),
    ("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env", "${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/migrate.env",
     "compose_env_file_outside_runtime:webapi"),
    ("${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env", "${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/bot.env",
     "compose_env_file_outside_runtime:webapi"),
    ("      - ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env",
     "      - path: ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env\n        required: false",
     "compose_env_file_optional:webapi"),
    ("container_name: bfx-webapi", "container_name: bfx-api", "compose_container_name:webapi"),
    ("    container_name: bfx-webapi\n", "", "compose_container_name_missing:webapi"),
    ("      - ${BFX_RUNTIME_DIR:-/opt/bfx/runtime}/webapi.env\n", "",
     "compose_env_files_not_rendered:webapi"),
    ("  webapi:\n", "  api:\n", "compose_service_missing:webapi"),
    ("name: bfx-app\n", "name: other\n", "compose_project_name"),
])
def test_a_compose_file_the_deployer_cannot_run_is_refused_before_any_change(
    harness: Harness, old: str, new: str, code: str,
) -> None:
    assert COMPOSE_TEXT.count(old) == 1
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = COMPOSE_TEXT.replace(old, new)
    assert harness.run() == 1
    assert harness.ledger.last.detail == f"precheck:{code}; running release untouched"
    assert harness.host.count(PULL) == harness.host.count(COMPOSE_UP) == 0


def test_a_third_party_image_is_pulled_by_digest_without_the_registry_token(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT, configs=False)
    deployer = harness.deployer()
    deployer._registry_credentials = ("will", "ghcr-token")
    assert deployer.run() == 0
    host = harness.host
    assert sorted(host.pulls) == sorted([(f"{BACKEND}@{NEW_B}", True), (f"{FRONTEND}@{NEW_F}", True),
                                         (TAILSCALE, False)])
    assert host.calls[host.index(COMPOSE_UP)][-4:] == ("bot", "frontend", "tailscale", "webapi")
    assert host.running["tailscale"][0] == TAILSCALE
    # bfx-deploy verified it like the others (name, project, image), without identity env.
    inspected = host.calls[host.index(_is(("docker", "inspect", "--type", "container", "bfx-bot")))]
    assert "bfx-tailscale" in inspected


def test_a_third_party_container_on_another_image_fails_the_deploy(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT, configs=False)
    original = harness.host._compose

    def compose(call: tuple[str, ...], env: Mapping[str, str] | None) -> Any:
        result = original(call, env)
        if COMPOSE_UP(call) and "tailscale" in harness.host.running:
            harness.host.running["tailscale"] = (TAILSCALE.replace("c" * 64, "d" * 64), "", "", "")
        return result

    monkeypatch.setattr(harness.host, "_compose", compose)
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "rolled_back"
    assert row.detail.startswith("unhealthy:container_release_mismatch:tailscale:image;")


def test_pruning_never_removes_a_third_party_image(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT, configs=False)
    harness.host.local_digests = {BACKEND: [OLD_B, NEW_B, "sha256:" + "8" * 64], FRONTEND: [OLD_F, NEW_F]}
    assert harness.run() == 0
    removed = [call[-1] for call in harness.host.calls if call[:3] == ("docker", "image", "rm")]
    assert removed == [f"{BACKEND}@sha256:" + "8" * 64]
    listed = [call[-1] for call in harness.host.calls if call[:3] == ("docker", "image", "ls")]
    assert set(listed) == {BACKEND, FRONTEND}


@pytest.mark.parametrize("image", [
    "tailscale/tailscale@sha256:" + "c" * 64,                    # Compose prints it as written
    "docker.io/tailscale/tailscale:v1.102.5",
    "docker.io/library/busybox@sha256:" + "c" * 64,
    "ghcr.io/will413028/bfx-funding-bot-backend@sha256:" + "9" * 64,
])
def test_an_image_outside_the_release_and_the_allowlist_is_refused(harness: Harness, image: str) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT, configs=False) \
        .replace(TAILSCALE, image)
    assert harness.run() == 1
    assert harness.ledger.last.detail == ("precheck:compose_image_not_allowed:tailscale; "
                                          "running release untouched")
    assert harness.host.pulls == []


def test_a_config_file_the_compose_file_references_is_written_from_its_revision(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT)
    harness.host.files[(REV_NEW, SERVE_PATH)] = '{"TCP": {"443": {"HTTPS": true}}}\n'
    assert harness.run() == 0
    written = _release(harness) / "tailscale/serve.json"
    assert written.read_text() == '{"TCP": {"443": {"HTTPS": true}}}\n'
    assert (written.stat().st_mode & 0o777) == 0o600


def test_a_revision_without_configs_writes_nothing_more(harness: Harness) -> None:
    harness.host.files[(REV_NEW, SERVE_PATH)] = "{}\n"     # tracked, but not referenced
    assert harness.run() == 0
    assert sorted(p.name for p in _release(harness).iterdir()) == ["docker-compose.app.yml", "live.env"]


def test_rollback_to_a_revision_without_the_config_file_works(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT)
    harness.host.files[(REV_NEW, SERVE_PATH)] = "{}\n"
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "rolled_back", row.detail
    assert "tailscale" not in harness.host.running         # the orphan of the failed release
    assert not (_release(harness, REV_OLD) / "tailscale").exists()


def test_recreate_of_a_revision_without_configs_works(harness: Harness) -> None:
    assert harness.run(recreate=True) == 0
    assert harness.ledger.last.outcome == "deployed"
    assert sorted(p.name for p in _release(harness, REV_OLD).iterdir()) == [
        "docker-compose.app.yml", "live.env"]


def test_recreate_writes_the_config_file_its_revision_references(harness: Harness) -> None:
    harness.host.files[(REV_OLD, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT)
    harness.host.files[(REV_OLD, SERVE_PATH)] = "{}\n"
    assert harness.run(recreate=True) == 0
    assert (_release(harness, REV_OLD) / "tailscale/serve.json").read_text() == "{}\n"


def test_a_referenced_config_file_the_revision_lacks_fails_the_precheck(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT)
    harness.host.current = ("h1",)                       # even with a migration pending
    assert harness.run() == 1
    assert harness.ledger.last.detail == (f"precheck:release_file_missing:{SERVE_PATH}; "
                                          "running release untouched")
    assert harness.host.stopped == [] and harness.host.count(COMPOSE_UP) == 0


@pytest.mark.parametrize(("change", "code"), [
    (lambda t: t.replace("file: ./tailscale/serve.json", "file: ../../serve.json"),
     "compose_config_outside_release:ts-serve"),
    (lambda t: t.replace("file: ./tailscale/serve.json", "file: /etc/passwd"),
     "compose_config_outside_release:ts-serve"),
    (lambda t: t.replace("file: ./tailscale/serve.json", 'content: "{}"'),
     "compose_config_not_file:ts-serve"),
    (lambda t: t.replace("file: ./tailscale/serve.json", "environment: HOME"),
     "compose_config_not_file:ts-serve"),
    (lambda t: t + "secrets:\n  key:\n    file: ./tailscale/serve.json\n", "compose_secrets_refused"),
])
def test_a_config_that_is_not_a_release_file_is_refused(
    harness: Harness, change: Callable[[str], str], code: str,
) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = change(with_sidecar(COMPOSE_TEXT))
    harness.host.files[(REV_NEW, SERVE_PATH)] = "{}\n"
    assert harness.run() == 1
    assert harness.ledger.last.detail == f"precheck:{code}; running release untouched"
    assert sorted(p.name for p in _release(harness).iterdir()) == ["docker-compose.app.yml", "live.env"]


def test_a_config_tracked_as_a_symlink_is_refused(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT)
    harness.host.files[(REV_NEW, SERVE_PATH)] = "/etc/shadow"
    harness.host.symlinks.add((REV_NEW, SERVE_PATH))
    assert harness.run() == 1
    assert harness.ledger.last.detail == (f"precheck:release_file_not_regular:{SERVE_PATH}; "
                                          "running release untouched")


def test_a_foreign_frontend_does_not_block_a_release_without_one(harness: Harness) -> None:
    _without_frontend(harness)
    harness.host.foreign = ["bfx-frontend"]
    assert harness.run() == 0
    assert harness.ledger.last.outcome == "deployed"


# --------------------------------------------------------------------------- health probes


def test_the_webapi_is_probed_on_ready_which_reads_the_database(harness: Harness) -> None:
    assert harness.run() == 0
    probes = set(harness.host.probes)
    assert probes == {"http://127.0.0.1:8080/healthz", "http://127.0.0.1:8000/ready"}


def test_a_sidecar_that_never_turns_healthy_fails_the_deploy(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT, configs=False)
    harness.host.sidecar_health = "unhealthy"
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "rolled_back"
    assert row.detail.startswith("unhealthy:health_timeout:tailscale;")


@pytest.mark.parametrize("healthcheck", [
    "",
    "    healthcheck:\n      disable: true\n",
])
def test_a_service_with_no_probe_of_its_own_is_refused(harness: Harness, healthcheck: str) -> None:
    declared = SIDECAR[SIDECAR.index("    healthcheck:\n"):SIDECAR.index("    configs:\n")]
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT, configs=False) \
        .replace(declared, healthcheck)
    assert harness.run() == 1
    assert harness.ledger.last.detail == ("precheck:compose_healthcheck_missing:tailscale; "
                                          "running release untouched")
    assert harness.host.pulls == []


# --------------------------------------------------------------------------- netns


def test_the_web_api_and_the_sidecar_it_joins_are_created_in_one_up(harness: Harness) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = JOINED_TEXT
    assert harness.run() == 0
    host = harness.host
    assert host.count(COMPOSE_UP) == 1
    assert host.calls[host.index(COMPOSE_UP)][-4:] == ("bot", "frontend", "tailscale", "webapi")
    assert host.meta["webapi"]["NetworkMode"] == "container:" + host.meta["tailscale"]["Id"]
    assert harness.ledger.last.outcome == "deployed"


@pytest.mark.parametrize("how", ["recreated", "restarted"])
def test_a_web_api_left_in_a_dead_namespace_fails_the_deploy(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, how: str,
) -> None:
    harness.host.files[(REV_NEW, bfx.COMPOSE_PATH)] = JOINED_TEXT
    original = harness.host._compose

    def compose(call: tuple[str, ...], env: Mapping[str, str] | None) -> Any:
        result = original(call, env)
        if COMPOSE_UP(call) and "tailscale" in harness.host.running:
            # A Compose that left the web API behind its sidecar (recreated, or started again).
            if how == "recreated":
                harness.host.create("tailscale")
            else:
                harness.host.restart_in_place("tailscale")
        return result

    monkeypatch.setattr(harness.host, "_compose", compose)
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "rolled_back"
    assert row.detail.startswith("unhealthy:container_release_mismatch:webapi:netns;")


@pytest.mark.parametrize(("joined", "provider", "stale"), [
    ({"NetworkMode": "container:x", "StartedAt": "2026-10-20T08:00:01.5Z"},
     {"Id": "x", "StartedAt": "2026-10-20T08:00:01.25Z"}, False),
    # trailing zeros trimmed: .5 is later than .25 although "5" < "25" as text
    ({"NetworkMode": "container:x", "StartedAt": "2026-10-20T08:00:01.25Z"},
     {"Id": "x", "StartedAt": "2026-10-20T08:00:01.5Z"}, True),
    ({"NetworkMode": "container:x", "StartedAt": "2026-10-20T08:00:02Z"},
     {"Id": "x", "StartedAt": "2026-10-20T08:00:01.999999999Z"}, False),
    ({"NetworkMode": "container:old", "StartedAt": "2026-10-20T08:00:02Z"},
     {"Id": "x", "StartedAt": "2026-10-20T08:00:01Z"}, True),
    ({"NetworkMode": "container:x", "StartedAt": "garbage"},
     {"Id": "x", "StartedAt": "2026-10-20T08:00:01Z"}, True),
])
def test_netns_staleness(joined: dict[str, str], provider: dict[str, str], stale: bool) -> None:
    def record(container: dict[str, str]) -> dict[str, Any]:
        return {"Id": container.get("Id", ""), "State": {"StartedAt": container["StartedAt"]},
                "HostConfig": {"NetworkMode": container.get("NetworkMode", "bfx_default")}}

    assert bfx.netns_stale(record(joined), record(provider)) is stale


def _joined(harness: Harness) -> None:
    """The running release (REV_OLD, what main still names) has the web API in the sidecar."""
    harness.registry.refs[f"{BACKEND}:main"] = OLD_B
    harness.host.files[(REV_OLD, bfx.COMPOSE_PATH)] = JOINED_TEXT
    harness.host.run_joined()


def _break(harness: Harness, how: str) -> None:
    if how == "recreated":
        harness.host.create("tailscale")
    else:
        harness.host.restart_in_place("tailscale")


def _repair(call: tuple[str, ...]) -> bool:
    """The netns repair's `up`: the web API alone."""
    return COMPOSE_UP(call) and call[-1] == "webapi"

NETNS_STATE = "netns-repair.json"


def _bot_untouched(harness: Harness) -> bool:
    return not any("bfx-bot" in call or (COMPOSE_UP(call) and "bot" in call)
                   for call in harness.host.calls)


def test_nothing_is_repaired_while_the_namespace_is_live(harness: Harness) -> None:
    _joined(harness)
    assert harness.run() == 0
    assert harness.host.calls == [NETNS_INSPECT] and harness.notices == []
    assert not (harness.settings.state_dir / NETNS_STATE).exists()


@pytest.mark.parametrize("how", ["recreated", "restarted"])
def test_a_dead_namespace_is_repaired_by_recreating_the_web_api_alone(harness: Harness, how: str) -> None:
    _joined(harness)
    _break(harness, how)
    rows = len(harness.ledger.rows)
    assert harness.run() == 0
    host = harness.host
    assert host.count(COMPOSE_UP) == 1
    assert host.calls[host.index(COMPOSE_UP)][6:] == ("up", "--detach", "--no-deps", "--force-recreate",
                                                     "webapi")
    env = host.envs[host.index(COMPOSE_UP)]
    assert env is not None
    assert (env["BFX_BACKEND_DIGEST"], env["BFX_SOURCE_REVISION"], env["BFX_DEPLOYMENT_ID"]) == (
        OLD_B, REV_OLD, OLD_ATTEMPT)
    assert host.meta["webapi"]["NetworkMode"] == "container:" + host.meta["tailscale"]["Id"]
    assert not bfx.netns_stale(inspect_record("webapi", host.running["webapi"], host.meta["webapi"]),
                               inspect_record("tailscale", host.running["tailscale"], host.meta["tailscale"]))
    assert "http://127.0.0.1:8000/ready" in host.probes
    assert _bot_untouched(harness)
    assert len(harness.ledger.rows) == rows                 # same digest and revision: no row
    assert [level for level, _ in harness.notices] == ["info"]
    assert not (harness.settings.state_dir / NETNS_STATE).exists()


def test_the_already_attempted_branch_repairs_too(harness: Harness) -> None:
    _joined(harness)
    harness.registry.refs[f"{BACKEND}:main"] = NEW_B
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, "rolled_back", attempt_id="n")
    _break(harness, "restarted")
    assert harness.run() == 0
    assert harness.host.count(_repair) == 1 and harness.host.count(COMPOSE_UP) == 1


@pytest.mark.parametrize("outcome", ["failed", "started"])
def test_no_repair_after_a_failed_or_interrupted_attempt(harness: Harness, outcome: str) -> None:
    # A failed migration path keeps the bot and the web API stopped on purpose.
    _joined(harness)
    harness.registry.refs[f"{BACKEND}:main"] = NEW_B
    harness.ledger.seed(REV_NEW, NEW_B, NEW_F, outcome, attempt_id="n")
    _break(harness, "recreated")
    assert harness.run() == 0
    assert harness.host.calls == []


def test_a_database_outage_alone_repairs_nothing(harness: Harness) -> None:
    _joined(harness)
    harness.host.unhealthy = {OLD_B}                  # /ready answers 503
    assert harness.run() == 0
    assert harness.host.calls == [NETNS_INSPECT]


def test_a_web_api_on_its_own_network_is_never_repaired(harness: Harness) -> None:
    # 9d-4 to 9d-5: the sidecar runs, the web API is still on bfx_default.
    harness.registry.refs[f"{BACKEND}:main"] = OLD_B
    harness.host.files[(REV_OLD, bfx.COMPOSE_PATH)] = with_sidecar(COMPOSE_TEXT, configs=False)
    harness.host.running["tailscale"] = (TAILSCALE, "", "", "")
    harness.host.create("tailscale")                  # started after the web API
    assert harness.run() == 0
    assert harness.host.calls == [NETNS_INSPECT]


@pytest.mark.parametrize("stopped", ["webapi", "tailscale"])
def test_a_stopped_web_api_or_sidecar_is_left_alone(harness: Harness, stopped: str) -> None:
    _joined(harness)
    _break(harness, "recreated")
    harness.host.parked[stopped] = harness.host.running.pop(stopped)
    assert harness.run() == 0
    assert harness.host.calls == [NETNS_INSPECT] and harness.notices == []


def test_failed_repairs_never_touch_the_bot_and_stop_after_three(harness: Harness) -> None:
    _joined(harness)
    _break(harness, "recreated")
    harness.host.compose_fail = {OLD_B}               # the web API cannot be recreated
    for attempt in (1, 2, 3):
        assert harness.run() == 0
        assert harness.host.count(_repair) == attempt
        state = json.loads((harness.settings.state_dir / NETNS_STATE).read_text())
        assert state["failures"] == attempt
    assert [level for level, _ in harness.notices] == ["warning", "warning", "critical"]
    assert "compose_up_failed" in harness.notices[-1][1]
    assert harness.run() == 0                          # a fourth tick: no more attempts
    assert harness.host.count(_repair) == 3 and len(harness.notices) == 3
    assert _bot_untouched(harness) and harness.host.stopped == []
    # The sidecar starts anew: a new incident, counted from zero.
    _break(harness, "restarted")
    assert harness.run() == 0
    assert harness.host.count(_repair) == 4
    assert json.loads((harness.settings.state_dir / NETNS_STATE).read_text())["failures"] == 1


def test_an_escalated_incident_that_recovers_says_so_and_forgets_it(harness: Harness) -> None:
    _joined(harness)
    _break(harness, "recreated")
    harness.host.compose_fail = {OLD_B}
    for _ in range(3):
        harness.run()
    harness.host.create("webapi", "container:" + harness.host.meta["tailscale"]["Id"])  # operator
    assert harness.run() == 0
    assert harness.notices[-1] == ("info", "bfx-webapi is in the sidecar's network namespace again")
    assert not (harness.settings.state_dir / NETNS_STATE).exists()


def test_a_repaired_namespace_whose_web_api_is_not_ready_is_not_repaired_again(harness: Harness) -> None:
    # The database is down while the repair runs: the namespace is right, /ready is not.
    _joined(harness)
    _break(harness, "recreated")
    harness.host.unhealthy = {OLD_B}
    assert harness.run() == 0
    assert [level for level, _ in harness.notices] == ["warning"]
    assert harness.run() == 0
    assert harness.host.count(_repair) == 1
    assert not (harness.settings.state_dir / NETNS_STATE).exists()


def test_an_unreadable_repair_record_counts_as_none(harness: Harness) -> None:
    _joined(harness)
    _break(harness, "recreated")
    path = harness.settings.state_dir / NETNS_STATE
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert harness.run() == 0
    assert harness.host.count(_repair) == 1 and not path.exists()


def test_a_dry_run_only_reports_a_dead_namespace(harness: Harness) -> None:
    _joined(harness)
    _break(harness, "recreated")
    assert harness.run(dry_run=True) == 0
    assert harness.host.calls == [NETNS_INSPECT] and harness.notices == []
    assert not (harness.settings.state_dir / NETNS_STATE).exists()


# --------------------------------------------------------------------------- hung commands


def test_a_docker_stop_that_times_out_is_a_failed_stop_not_a_crash(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.timeouts = {("docker", "stop", "--time", "30", "bfx-bot")}
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "failed"
    assert row.detail == "bot_stop_failed; nothing migrated; running release untouched; webapi restarted"
    assert not any("crashed" in text for _, text in harness.notices)


def test_a_docker_start_that_times_out_is_a_failed_start_not_a_crash(harness: Harness) -> None:
    harness.host.current = ("h1",)
    harness.host.stop_fails = {"bfx-bot"}
    harness.host.timeouts = {("docker", "start", "bfx-webapi")}
    assert harness.run() == 1
    assert harness.ledger.last.detail.startswith("bot_stop_failed; nothing migrated; webapi_restart_failed:"
                                                 "start_failed")
    assert not any("crashed" in text for _, text in harness.notices)


def test_a_hung_probe_is_an_unhealthy_one_and_the_attempt_gets_its_terminal_row(harness: Harness) -> None:
    harness.host.timeouts = {("docker", "exec", "bfx-webapi")}
    assert harness.run() == 1
    started = _started(harness)[-1]
    assert harness.ledger.attempt(started.attempt_id)[-1] == "failed"
    assert "health_timeout:webapi" in harness.ledger.last.detail
    assert not any("crashed" in text for _, text in harness.notices)


# --------------------------------------------------------------------------- rollback schema


def test_rollback_onto_a_schema_past_the_previous_release_is_refused(harness: Harness) -> None:
    # An earlier attempt migrated to h3 and failed; this one (a --retry or a later release) has
    # nothing left to migrate and is unhealthy. The previous release was written for h2.
    harness.host.heads, harness.host.current = ("h3",), ("h3",)
    harness.host.heads_by_digest = {OLD_B: ("h2",)}
    harness.host.unhealthy = {NEW_B}
    assert harness.run() == 1
    row = harness.ledger.last
    assert row.outcome == "failed"
    assert "rollback_failed:rollback_schema_mismatch" in row.detail
    host = harness.host
    assert host.count(COMPOSE_UP) == 1                  # the release's own; the old one never came back
    assert host.running["webapi"][1] == NEW_B and host.stopped == ["bfx-bot"]


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
    assert "BFX_CHANGE_CLASS" not in env
    started, finished = harness.ledger.rows[-2:]
    assert (started.outcome, finished.outcome) == ("started", "deployed")
    assert env["BFX_DEPLOYMENT_ID"] == started.attempt_id == finished.attempt_id != OLD_ATTEMPT
    assert finished.detail == "recreate; ok"
    assert host.count(STOP_ANY) == 0 and host.count(BACKUP) == 0 and host.count(UV_SYNC) == 0


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
    # Whether the revision needs a frontend image is its compose file's call: it does.
    assert harness.host.count(PULL) == harness.host.count(COMPOSE_UP) == 0
    assert harness.notices == []
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
    assert plan["stop_webapi_before_migration"] is True
    assert plan["restore_test_before_deploy"] == "migration_pending"
    assert harness.host.count(RESTORE_TEST) == 0
    assert plan["rollback_target"] == REV_OLD
    assert not {"change_class", "class_detail"} & set(plan)
    assert plan["blockers"] == [f"foreign_container_holds_name:{n}"
                                for n in ("bfx-bot", "bfx-webapi", "bfx-frontend")]


# --------------------------------------------------------------------------- container check unit


def _target() -> Any:
    return bfx.Target(REV_NEW, NEW_B, NEW_F, f"{BACKEND}@{NEW_B}", f"{FRONTEND}@{NEW_F}")


def _mismatches(record: dict[str, Any], service: str, target: Any, deployment_id: str) -> list[str]:
    image = target.frontend_image if service == "frontend" else target.backend_image
    return list(bfx.container_mismatches(
        record, container=CONTAINER_NAMES[service], image=image,
        identity=bfx.identity_env(target, image=image, deployment_id=deployment_id)))


@pytest.mark.parametrize("service", ["bot", "webapi", "frontend"])
def test_container_running_the_recorded_release_has_no_mismatch(service: str) -> None:
    target = _target()
    backend = service != "frontend"
    running = (target.backend_image if backend else target.frontend_image,
               NEW_B if backend else NEW_F, REV_NEW, "dep-1")
    assert _mismatches(inspect_record(service, running), service, target, "dep-1") == []


@pytest.mark.parametrize("change_class", [None, "material", "standard", "retired"])
def test_identity_check_ignores_the_retired_change_class(change_class: str | None) -> None:
    # The previous bfx-deploy injects its own class; this one injects none and
    # the compose default fills in `retired`. None of it is identity any more.
    target = _target()
    record = inspect_record("bot", (target.backend_image, NEW_B, REV_NEW, "dep-1"))
    env = [e for e in record["Config"]["Env"] if not e.startswith("BFX_CHANGE_CLASS=")]
    record["Config"]["Env"] = env + ([f"BFX_CHANGE_CLASS={change_class}"] if change_class else [])
    assert _mismatches(record, "bot", target, "dep-1") == []
    assert set(bfx.identity_env(target, image=target.backend_image, deployment_id="dep-1") or {}) == {
        "BFX_IMAGE_DIGEST", "BFX_SOURCE_REVISION", "BFX_DEPLOYMENT_ID"}


def test_a_third_party_container_is_checked_by_name_project_and_image_alone() -> None:
    target = _target()
    assert bfx.identity_env(target, image=TAILSCALE, deployment_id="dep-1") is None
    record = inspect_record("tailscale", (TAILSCALE, "", "", ""))
    assert bfx.container_mismatches(record, container="bfx-tailscale", image=TAILSCALE,
                                    identity=None) == []
    record["Config"]["Image"] = TAILSCALE.replace("c" * 64, "d" * 64)
    assert bfx.container_mismatches(record, container="bfx-tailscale", image=TAILSCALE,
                                    identity=None) == ["image"]


@pytest.mark.parametrize(("mutate", "mismatch"), [
    (lambda r: r.__setitem__("Name", "/other"), "name"),
    (lambda r: r["Config"].__setitem__("Labels", {}), "project"),
    (lambda r: r["Config"].__setitem__("Image", f"{FRONTEND}@{NEW_B}"), "image"),
    (lambda r: r["Config"]["Env"].__setitem__(0, "BFX_IMAGE_DIGEST=" + NEW_B), "identity_env"),
    (lambda r: r["Config"]["Env"].__setitem__(1, "BFX_SOURCE_REVISION=" + REV_OLD), "identity_env"),
    (lambda r: r["Config"]["Env"].__setitem__(2, "BFX_DEPLOYMENT_ID=other"), "identity_env"),
])
def test_each_release_mismatch_is_reported(mutate: Callable[[dict[str, Any]], None], mismatch: str) -> None:
    target = _target()
    record = inspect_record("frontend", (target.frontend_image, NEW_F, REV_NEW, "dep-1"))
    mutate(record)
    assert mismatch in _mismatches(record, "frontend", target, "dep-1")


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
        migrations_applied=True, outcome="started", detail=detail))
    assert ledger_id == 7
    assert seen["argv"][:6] == ["docker", "exec", "-i", "--user", "postgres", "bfx-postgres"]
    assert f"detail={detail}" in seen["argv"] and "finished_at=" in seen["argv"]
    assert detail not in seen["sql"] and ":'detail'" in seen["sql"]
    # The retired change_class column is never written (left NULL until it is dropped).
    assert "change_class" not in seen["sql"]
    assert not any(arg.startswith("change_class=") for arg in seen["argv"])


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
