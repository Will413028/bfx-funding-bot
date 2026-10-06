#!/usr/bin/env python3
"""Deploy the newest green `main` release to the VM, by digest only.

Runs as root from bfx-deploy.timer (every 5 minutes) or by hand, with the
interpreter of its own uv environment (deploy/vm/ops/uv.lock). ADRs:
2026-09-25-ci-registry-digest-deploy (D1-D5) and
2026-09-25-automated-probation-replaces-release-ceremony (D6 backup before
migration); change classes are retired (lending-envelope plan D5).

One run:
  1. flock, so two runs never overlap;
  2. `docker buildx imagetools inspect backend:main`; stop if the ledger's newest
     successful deployment already runs that digest (or its newest attempt
     already tried it -- a failed digest is retried only with --retry);
  3. read the revision and CI-run labels, require `backend:sha-<rev>` to be the
     same digest and `frontend:sha-<rev>` to carry the same revision;
  4. fetch the VM mirror, require <rev> on origin/main, and take the compose
     file and live.env from <rev> itself;
  5. read `git diff --name-only --no-renames <last deployed>..<rev>` for the
     DR trigger paths (a diff that cannot be read counts as a DR change);
  6. pull both images by digest, validate the env files, compare `alembic
     current` with `alembic heads` in a one-shot of the new image;
  7. migrations pending: stop the bot (the writer), back up with <rev>'s
     backup.sh, run the isolated restore test with <rev>'s DR scripts, `alembic
     upgrade head`. Any failure from here keeps the bot stopped: roll forward.
     No migration but a DR-path change: restore test first, bot keeps running;
  8. append the `started` ledger row (the bot reads it at boot as its own
     deployment, BFX_DEPLOYMENT_ID), then `docker compose -p bfx-app up`, check
     each container runs the expected digest with the expected identity env,
     wait for health (bot healthz, webapi health, frontend 127.0.0.1:3001) and a
     settle window;
  9. on failure without applied migrations: recreate the previous successful
     digests (rolled_back). With applied migrations, or nothing to roll back to:
     stop the bot, never roll back (old code may not run on the new schema);
 10. on success: install <rev>'s host tooling and units (effective from the
     next run) and point the DR checkout `current` at <rev>;
 11. append the terminal ledger row and alert.

`--recreate` redeploys the running release (same digests) after a runtime env
change, through the same started row, identity check, health wait and ledger.

Container hardening (read-only rootfs, users, capabilities, ports, network) is
a property of deploy/vm/docker-compose.app.yml and is enforced in CI on the
rendered `docker compose config` (compose_policy.py); here only the digest and
identity each container runs with are re-checked.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import importlib.util
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import pathspec

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx_notify = _load("_bfx_ops_notify", _HERE / "bfx_notify.py")

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
ALEMBIC_REVISION = re.compile(r"[A-Za-z0-9_]{1,64}")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CI_RUN_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]{1,20}")
REVISION_LABEL = "org.opencontainers.image.revision"
CI_RUN_LABEL = "bfx.ci-run-url"
PROJECT = "bfx-app"
COMPOSE_PATH = "deploy/vm/docker-compose.app.yml"
LIVE_ENV_PATH = "deploy/vm/live.env"
BACKUP_SCRIPT = "deploy/vm/pgbackrest/backup.sh"
TOOLING_PATHS = ("deploy/vm/ops", "deploy/vm/systemd")
SERVICES = ("bot", "webapi", "frontend")
# A change to how PostgreSQL is run, backed up or restored earns an isolated
# restore test before the release goes out (plan DR bullet, Will 2026-09-25), as
# does every pending migration and any diff that cannot be read. Hard-wired in
# the running tool (not read from the target), so a commit cannot switch its own
# trigger off; matched by path_matches below.
DR_TRIGGER_PATTERNS = (
    "deploy/vm/pgbackrest/**",
    "deploy/vm/postgres/**",
    "docker-compose.bot.yml",
    "docker-compose.dr.yml",
)
RESTORE_TEST_UNIT = "bfx-restore-test@{revision}.service"
CONTAINERS = {"bot": "bfx-bot", "webapi": "bfx-webapi", "frontend": "bfx-frontend"}
RUNTIME_ENV_FILES = {"bot": "bot.env", "webapi": "webapi.env", "frontend": "frontend.env"}
PROBE = (
    "import sys,urllib.request;"
    "sys.exit(0 if urllib.request.urlopen(sys.argv[1],timeout=5).status==200 else 1)"
)
IMAGETOOLS_FORMAT = '{"manifest":{{json .Manifest}},"image":{{json .Image}}}'
PLATFORM = "linux/arm64"
_MAX_DETAIL = 1800


def log(message: str) -> None:
    print(f"bfx-deploy: {message}", file=sys.stderr, flush=True)


@lru_cache(maxsize=16)
def _path_spec(patterns: tuple[str, ...]) -> pathspec.PathSpec[Any]:
    return pathspec.PathSpec.from_lines("gitignore", ["/" + pattern for pattern in patterns])


def path_matches(patterns: Sequence[str], path: str) -> bool:
    """Whether any repository-root-anchored glob in `patterns` matches `path`.

    pathspec's gitignore patterns, each prefixed with `/` (pinned in
    deploy/vm/ops/uv.lock): `*` and `?` stay inside one path segment, `**/`
    spans directories, a trailing `**` spans everything below, and a slash-less
    pattern such as `docker-compose.dr.yml` matches only at the root instead of
    floating to other directories as it would in a .gitignore.
    """
    return bool(patterns) and _path_spec(tuple(patterns)).match_file(path)


# --------------------------------------------------------------------------- errors


class DeployError(Exception):
    """A bounded failure code; never carries secrets or raw command output."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DiscoveryError(DeployError):
    """The release cannot be identified yet (registry, ledger read, CI mid-push)."""


class CommandError(DeployError):
    def __init__(self, code: str, *, timed_out: bool = False) -> None:
        super().__init__(code)
        self.timed_out = timed_out


# --------------------------------------------------------------------------- runner


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout: float,
        input_text: str | None = None, env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


def subprocess_runner(
    argv: Sequence[str], *, timeout: float,
    input_text: str | None = None, env: Mapping[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv), input=input_text, capture_output=True, text=True, check=False,
            timeout=timeout, env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired:
        raise CommandError(f"timeout:{_describe(argv)}", timed_out=True) from None
    except OSError as exc:
        raise CommandError(f"not_executable:{_describe(argv)}:{type(exc).__name__}") from None
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _describe(argv: Sequence[str]) -> str:
    """Name a command for logs and codes without its arguments' values."""
    words = [part for part in argv[:6] if not part.startswith("-") and "=" not in part]
    return " ".join(words[:3])


# --------------------------------------------------------------------------- files


def parse_env_file(text: str, *, name: str, compose: bool) -> dict[str, str]:
    """KEY=VALUE lines; with compose=True, refuse values Compose would rewrite.

    The runtime secret files predate Compose and were written for `docker
    --env-file`, which takes values literally. Compose's env_file interpolates
    `$`, strips surrounding quotes and drops ` #` comments, so such a value
    would silently become a different secret. Error codes name the file and
    key, never the value.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or ENV_KEY.fullmatch(key) is None:
            raise DeployError(f"env_file_invalid_line:{name}")
        if key in values:
            raise DeployError(f"env_file_duplicate_key:{name}:{key}")
        if compose and (
            value != value.strip()
            or value[:1] in {"'", '"'}
            or "$" in value
            or " #" in value
            or "\t#" in value
        ):
            raise DeployError(f"env_file_value_ambiguous_for_compose:{name}:{key}")
        values[key] = value
    return values


def protected_secret_file(path: Path, *, owner_uid: int = 0) -> None:
    """A secret file only root can read: regular, not a symlink, root-owned, 0600.

    The mirror and the DR checkouts belong to the unprivileged `ubuntu` user,
    whose code the DR units run; a secret readable by that user would widen who
    holds the venue key, the vault KEK and the database owner password.
    """
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise DeployError(f"secret_file_missing:{path.name}")
    info = path.lstat()
    if info.st_uid != owner_uid:
        raise DeployError(f"secret_file_not_root_owned:{path.name}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise DeployError(f"secret_file_mode_not_0600:{path.name}")


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_atomic(path, content, 0o600)


def _write_atomic(path: Path, content: str, mode: int) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _readable_by_all(root: Path) -> None:
    """World-readable tree despite the service's UMask=0077.

    The restore-test unit runs as `ubuntu` and executes the tooling tree's
    interpreter; the isolated restore reads the DR checkout as another uid.
    Write access stays with the owner.
    """
    for directory, _, files in os.walk(root):
        os.chmod(directory, 0o755)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                mode = stat.S_IMODE(path.stat().st_mode)
                os.chmod(path, 0o755 if mode & 0o100 else 0o644)


def managed_units(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if re.fullmatch(r"bfx-[a-z0-9-]+@?\.(service|timer)", name) is None:
            raise DeployError("managed_units_invalid")
        names.append(name)
    if not names:
        raise DeployError("managed_units_empty")
    return names


def _point_symlink(link: Path, target: str) -> None:
    """Atomically make `link` a symlink to `target` (relative to its directory)."""
    temporary = link.with_name(f".{link.name}.{secrets.token_hex(4)}")
    os.symlink(target, temporary)
    try:
        os.replace(temporary, link)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def docker_auth(
    state_dir: Path, credentials: tuple[str, str] | None, hosts: Sequence[str],
) -> Iterator[dict[str, str]]:
    """A throwaway DOCKER_CONFIG so the GHCR token is never left in a docker login."""
    if credentials is None:
        yield {}
        return
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="docker-auth-", dir=state_dir))
    try:
        auth = base64.b64encode(":".join(credentials).encode()).decode()
        _write_private(directory / "config.json",
                       json.dumps({"auths": {host: {"auth": auth} for host in sorted(set(hosts))}}))
        yield {**_base_env(), "DOCKER_CONFIG": str(directory)}
    finally:
        shutil.rmtree(directory)


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class ImageInfo:
    digest: str
    labels: Mapping[str, str]


class Registry(Protocol):
    def inspect(self, reference: str) -> ImageInfo: ...


class ImagetoolsRegistry:
    """Tags, digests and labels through `docker buildx imagetools inspect`.

    The digest is the one the registry serves for the reference (the index for
    a multi-platform push); labels are those of the linux/arm64 image config.
    """

    def __init__(
        self, runner: Runner, auth: Callable[[], AbstractContextManager[dict[str, str]]],
        platform: str = PLATFORM,
    ) -> None:
        self._runner = runner
        self._auth = auth
        self._platform = platform

    def inspect(self, reference: str) -> ImageInfo:
        with self._auth() as env:
            result = self._runner(
                ["docker", "buildx", "imagetools", "inspect", reference, "--format", IMAGETOOLS_FORMAT],
                timeout=120.0, env=env or None)
        if result.returncode != 0:
            tail = result.stderr.strip()[-300:]
            log(f"imagetools inspect {reference} exited {result.returncode}: {tail}")
            if "not found" in tail.lower() or "manifest unknown" in tail.lower():
                raise DiscoveryError(f"registry_not_found:{reference.rsplit('/', 1)[-1][:120]}")
            raise DiscoveryError("registry_inspect_failed")
        try:
            payload = json.loads(result.stdout)
            digest = payload["manifest"]["digest"]
            image = payload["image"]
            config = image if "config" in image else image[self._platform]
            labels = config["config"].get("Labels") or {}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DiscoveryError("registry_inspect_unparsable") from None
        if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None or not isinstance(labels, dict):
            raise DiscoveryError("registry_inspect_unparsable")
        return ImageInfo(digest=digest, labels={str(k): str(v) for k, v in labels.items()})


# --------------------------------------------------------------------------- ledger


@dataclass(frozen=True, slots=True)
class LedgerRow:
    id: int
    attempt_id: str
    source_revision: str
    backend_digest: str
    frontend_digest: str
    outcome: str
    migrations_applied: bool
    ci_run: str | None = None


@dataclass(frozen=True, slots=True)
class LedgerView:
    exists: bool
    last_attempt: LedgerRow | None
    last_success: LedgerRow | None


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    attempt_id: str
    started_at: str
    finished_at: str | None  # None exactly for the `started` row
    source_revision: str
    backend_digest: str
    frontend_digest: str
    migrations_applied: bool
    outcome: str
    detail: str
    ci_run: str | None = None


class Ledger(Protocol):
    def read(self) -> LedgerView: ...
    def append(self, entry: LedgerEntry) -> int: ...


_LEDGER_COLUMNS = (
    "id, attempt_id, source_revision, backend_digest, frontend_digest, outcome, "
    "migrations_applied, ci_run"
)
# last_attempt is the newest row of any phase: a `started` row with no terminal
# row is an interrupted attempt and counts as tried (no automatic retry).
_LEDGER_READ = f"""SELECT to_regclass('public.deployments') IS NOT NULL AS present \\gset
\\if :present
SELECT json_build_object(
  'last_attempt', (SELECT row_to_json(d) FROM (SELECT {_LEDGER_COLUMNS}
     FROM public.deployments ORDER BY id DESC LIMIT 1) d),
  'last_success', (SELECT row_to_json(d) FROM (SELECT {_LEDGER_COLUMNS}
     FROM public.deployments WHERE outcome = 'deployed' ORDER BY id DESC LIMIT 1) d));
\\else
SELECT 'absent';
\\endif
"""
_LEDGER_APPEND = """INSERT INTO public.deployments (
  attempt_id, started_at, finished_at, source_revision, backend_digest, frontend_digest,
  migrations_applied, outcome, detail, ci_run)
VALUES (:'attempt_id', :'started_at', NULLIF(:'finished_at', '')::timestamptz,
  :'source_revision', :'backend_digest', :'frontend_digest',
  :'migrations_applied', :'outcome', :'detail', NULLIF(:'ci_run', ''))
RETURNING id;
"""


class PsqlLedger:
    """The deployments ledger through `docker exec <postgres> psql` as the owner role.

    Values travel as psql variables and are quoted by psql (`:'name'`), never
    formatted into SQL. No new database account is involved.
    """

    def __init__(self, runner: Runner, *, container: str, db_user: str, db_name: str) -> None:
        self._runner = runner
        self._container = container
        self._db_user = db_user
        self._db_name = db_name

    def _psql(self, sql: str, variables: Mapping[str, str] | None = None) -> str:
        argv = ["docker", "exec", "-i", "--user", "postgres", self._container,
                "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                "-h", "/var/run/postgresql", "-U", self._db_user, "-d", self._db_name]
        for key, value in (variables or {}).items():
            argv += ["-v", f"{key}={value}"]
        result = self._runner(argv, input_text=sql, timeout=60)
        if result.returncode != 0:
            log(f"ledger psql failed: {result.stderr.strip()[-500:]}")
            raise CommandError(f"ledger_psql_exit_{result.returncode}")
        return result.stdout

    def read(self) -> LedgerView:
        output = self._psql(_LEDGER_READ).strip()
        if output == "absent":
            return LedgerView(exists=False, last_attempt=None, last_success=None)
        try:
            payload = json.loads(output.splitlines()[-1])
            return LedgerView(
                exists=True,
                last_attempt=_ledger_row(payload["last_attempt"]),
                last_success=_ledger_row(payload["last_success"]),
            )
        except (ValueError, KeyError, TypeError, IndexError):
            raise CommandError("ledger_read_invalid") from None

    def append(self, entry: LedgerEntry) -> int:
        output = self._psql(_LEDGER_APPEND, {
            "attempt_id": entry.attempt_id,
            "started_at": entry.started_at, "finished_at": entry.finished_at or "",
            "source_revision": entry.source_revision, "backend_digest": entry.backend_digest,
            "frontend_digest": entry.frontend_digest,
            "migrations_applied": "true" if entry.migrations_applied else "false",
            "outcome": entry.outcome, "detail": entry.detail, "ci_run": entry.ci_run or "",
        })
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines or not lines[-1].strip().isdigit():
            raise CommandError("ledger_append_unconfirmed")
        return int(lines[-1].strip())


def _ledger_row(value: object) -> LedgerRow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError
    ci_run = value.get("ci_run")
    return LedgerRow(
        id=int(value["id"]), attempt_id=str(value["attempt_id"]),
        source_revision=str(value["source_revision"]),
        backend_digest=str(value["backend_digest"]), frontend_digest=str(value["frontend_digest"]),
        outcome=str(value["outcome"]),
        migrations_applied=bool(value["migrations_applied"]),
        ci_run=str(ci_run) if ci_run is not None else None,
    )


# --------------------------------------------------------------------------- deployer


@dataclass(frozen=True, slots=True)
class Settings:
    mirror: Path = Path("/home/ubuntu/bfx-funding-bot")
    mirror_user: str = "ubuntu"
    remote: str = "origin"
    branch: str = "main"
    runtime_dir: Path = Path("/opt/bfx/runtime")
    state_dir: Path = Path("/var/lib/bfx-deploy")
    lock_file: Path = Path("/run/lock/bfx-deploy.lock")
    backend_repository: str = "ghcr.io/will413028/bfx-funding-bot-backend"
    frontend_repository: str = "ghcr.io/will413028/bfx-funding-bot-frontend"
    network: str = "bfx_default"
    postgres_container: str = "bfx-postgres"
    db_user: str = "bfx"
    db_name: str = "bfx"
    # Per-revision clean checkouts (git worktrees of the mirror, owned by the
    # mirror user) that the DR units and the pre-migration backup run from;
    # `current` points at the deployed release.
    dr_root: Path = Path("/home/ubuntu/bfx-releases")
    backup_user: str = "ubuntu"
    # Root-owned host tooling: releases/<rev>/{ops,systemd}, `current` -> the
    # deployed release. The wrappers and units execute current/ops.
    ops_root: Path = Path("/usr/local/lib/bfx-ops")
    unit_dir: Path = Path("/etc/systemd/system")
    uv: str = "/usr/local/bin/uv"
    system_python: str = "/usr/bin/python3"
    healthz_port: int = 8080
    frontend_url: str = "http://127.0.0.1:3001/"
    health_timeouts: Mapping[str, float] = field(
        default_factory=lambda: {"bot": 300.0, "webapi": 120.0, "frontend": 120.0}
    )
    health_interval: float = 5.0
    settle_seconds: float = 60.0
    discovery_alert_after: float = 1800.0
    local_alias: str | None = "bfx-bot:local"
    dry_run: bool = False
    retry: bool = False
    rollback_drill: bool = False
    recreate: bool = False


@dataclass(frozen=True, slots=True)
class Target:
    revision: str
    backend_digest: str
    frontend_digest: str
    backend_image: str
    frontend_image: str


@dataclass(frozen=True, slots=True)
class Prepared:
    release_dir: Path
    current: tuple[str, ...]
    heads: tuple[str, ...]
    changed_paths: tuple[str, ...] | None = None  # None: the diff could not be read

    @property
    def pending(self) -> bool:
        return set(self.current) != set(self.heads)

    @property
    def restore_test_reason(self) -> str | None:
        """Why this release needs an isolated restore test first, or None."""
        if self.pending:
            return "migration_pending"
        if self.changed_paths is None:
            return "diff_unavailable"
        touched = sorted(
            path for path in self.changed_paths if path_matches(DR_TRIGGER_PATTERNS, path)
        )
        return "dr_paths:" + ",".join(touched[:10]) if touched else None


@dataclass(slots=True)
class Attempt:
    started_at: str
    target: Target
    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ci_run: str | None = None
    action: str = "deploy"  # deploy | recreate
    migrations_applied: bool = False
    bot_stopped: bool = False
    started_recorded: bool = False


Notifier = Callable[[str, str], None]
HttpStatus = Callable[[str, float], int]


def http_status(url: str, timeout: float) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        return 0


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="seconds")


def _sanitize(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return cleaned[:_MAX_DETAIL]


def _alembic_revisions(output: str) -> tuple[str, ...]:
    revisions: list[str] = []
    for line in output.splitlines():
        words = line.split()
        if not words:
            continue
        if ALEMBIC_REVISION.fullmatch(words[0]) is None:
            raise DeployError("alembic_output_unparsable")
        revisions.append(words[0])
    return tuple(sorted(set(revisions)))


def identity_env(target: Target, *, service: str, deployment_id: str) -> dict[str, str]:
    backend = service != "frontend"
    return {
        "BFX_IMAGE_DIGEST": target.backend_digest if backend else target.frontend_digest,
        "BFX_SOURCE_REVISION": target.revision,
        "BFX_DEPLOYMENT_ID": deployment_id,
    }


def container_mismatches(
    record: Mapping[str, Any], *, service: str, target: Target, deployment_id: str,
) -> list[str]:
    """What bfx-deploy re-checks on a created container: the release it runs.

    Hardening is the compose file's job and is checked in CI (compose_policy.py);
    this proves Compose started exactly the digest and identity this attempt
    recorded in the ledger.
    """
    config = record.get("Config") or {}
    env: dict[str, str] = {}
    for item in config.get("Env") or []:
        key, _, value = str(item).partition("=")
        env[key] = value
    image = target.backend_image if service != "frontend" else target.frontend_image
    mismatches = []
    if record.get("Name") != "/" + CONTAINERS[service]:
        mismatches.append("name")
    if (config.get("Labels") or {}).get("com.docker.compose.project") != PROJECT:
        mismatches.append("project")
    if config.get("Image") != image:
        mismatches.append("image")
    expected = identity_env(target, service=service, deployment_id=deployment_id)
    if any(env.get(key) != value for key, value in expected.items()):
        mismatches.append("identity_env")
    return mismatches


class Deployer:
    def __init__(
        self, settings: Settings, *, runner: Runner, registry: Registry, ledger: Ledger,
        notify: Notifier, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep, http: HttpStatus = http_status,
        secret_check: Callable[[Path], None] = protected_secret_file,
        registry_credentials: tuple[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self._runner = runner
        self._registry = registry
        self._ledger = ledger
        self._notify = notify
        self._clock = clock
        self._sleep = sleep
        self._http = http
        self._secret_check = secret_check
        self._registry_credentials = registry_credentials
        self.plan: dict[str, Any] | None = None
        self._dry_run_blockers: list[str] = []

    # ------------------------------------------------------------------ commands

    def _run(
        self, argv: Sequence[str], *, timeout: float, input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return self._runner(argv, timeout=timeout, input_text=input_text, env=env)

    def _check(
        self, argv: Sequence[str], *, code: str, timeout: float,
        input_text: str | None = None, env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        result = self._run(argv, timeout=timeout, input_text=input_text, env=env)
        if result.returncode != 0:
            tail = result.stderr.strip()[-1500:]
            log(f"{code}: `{_describe(argv)}` exited {result.returncode}: {tail}")
            raise CommandError(code)
        return result

    def _as_mirror_user(self, *argv: str) -> list[str]:
        # The mirror's deploy key and ~/.ssh live in that user's home, and
        # root-owned objects would break the owner's own git later. runuser sets
        # HOME to the target user's home; nothing relies on root's.
        return ["runuser", "-u", self.settings.mirror_user, "--", *argv]

    def _git(self, *args: str, code: str, timeout: float = 120.0, at: Path | None = None) -> CommandResult:
        where = str(at or self.settings.mirror)
        return self._check(self._as_mirror_user("git", "-C", where, *args), code=code, timeout=timeout)

    def _git_try(self, *args: str, timeout: float = 60.0, at: Path | None = None) -> CommandResult:
        """For git calls whose non-zero exit is an answer, not an error."""
        where = str(at or self.settings.mirror)
        return self._run(self._as_mirror_user("git", "-C", where, *args), timeout=timeout)

    # ------------------------------------------------------------------ entry

    def run(self) -> int:
        with self._locked() as acquired:
            if not acquired:
                log("another bfx-deploy run holds the lock; nothing to do")
                return 0
            try:
                return self._recreate() if self.settings.recreate else self._run_locked()
            except Exception as exc:
                code = exc.code if isinstance(exc, DeployError) else type(exc).__name__
                log(f"unexpected failure: {code}")
                if not self.settings.dry_run:
                    self._notify("critical", f"bfx-deploy crashed ({code}); see journalctl -u bfx-deploy")
                return 1

    @contextmanager
    def _locked(self) -> Iterator[bool]:
        self.settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.lock_file.open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _run_locked(self) -> int:
        settings = self.settings
        started_at = _now_iso(self._clock)
        try:
            main = self._registry.inspect(f"{settings.backend_repository}:{settings.branch}")
            view = self._ledger.read()
        except DeployError as exc:
            return self._discovery_failed(exc)
        if view.last_success is not None and view.last_success.backend_digest == main.digest:
            self._discovery_ok()
            log(f"up to date: {view.last_success.source_revision} ({main.digest})")
            return 0
        attempted = view.last_attempt
        if attempted is not None and attempted.backend_digest == main.digest and not settings.retry:
            self._discovery_ok()
            log(f"{main.digest} was already attempted (ledger #{attempted.id}, {attempted.outcome}); "
                "waiting for a new release or --retry")
            return 0
        try:
            target = self._discover(main)
        except DeployError as exc:
            return self._discovery_failed(exc)
        self._discovery_ok()
        ci_run = main.labels.get(CI_RUN_LABEL)
        attempt = Attempt(started_at=started_at, target=target,
                          ci_run=ci_run if ci_run and CI_RUN_URL.fullmatch(ci_run) else None)
        return self._deploy(view, attempt)

    def _discover(self, main: ImageInfo) -> Target:
        settings = self.settings
        revision = main.labels.get(REVISION_LABEL, "")
        if REVISION.fullmatch(revision) is None:
            raise DiscoveryError("registry_revision_label_missing")
        if self._registry.inspect(f"{settings.backend_repository}:sha-{revision}").digest != main.digest:
            raise DiscoveryError("registry_main_and_revision_tag_disagree")
        frontend = self._registry.inspect(f"{settings.frontend_repository}:sha-{revision}")
        if frontend.labels.get(REVISION_LABEL) != revision:
            raise DiscoveryError("registry_frontend_revision_mismatch")
        return self._target(revision, main.digest, frontend.digest)

    def _target(self, revision: str, backend_digest: str, frontend_digest: str) -> Target:
        return Target(
            revision=revision, backend_digest=backend_digest, frontend_digest=frontend_digest,
            backend_image=f"{self.settings.backend_repository}@{backend_digest}",
            frontend_image=f"{self.settings.frontend_repository}@{frontend_digest}",
        )

    # ------------------------------------------------------------------ discovery state

    def _discovery_state_path(self) -> Path:
        return self.settings.state_dir / "discovery-failure.json"

    def _discovery_failed(self, exc: DeployError) -> int:
        log(f"release not discoverable yet: {exc.code}")
        if self.settings.dry_run:
            return 1
        path = self._discovery_state_path()
        now = self._clock()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            since, notified = float(state["since"]), bool(state["notified"])
        except (OSError, ValueError, KeyError, TypeError):
            since, notified = now, False
        if not notified and now - since >= self.settings.discovery_alert_after:
            minutes = int((now - since) // 60)
            self._notify("warning", f"bfx-deploy cannot identify or read the release for {minutes} min "
                         f"({exc.code}); no deploy is happening")
            notified = True
        _write_private(path, json.dumps({"since": since, "notified": notified, "code": exc.code}))
        return 1

    def _discovery_ok(self) -> None:
        if self.settings.dry_run:
            return
        path = self._discovery_state_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            state = {}
        if isinstance(state, dict) and state.get("notified"):
            self._notify("info", "bfx-deploy can identify the release again")
        path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ deploy

    def _deploy(self, view: LedgerView, attempt: Attempt) -> int:
        target = attempt.target
        log(f"target {target.revision} backend {target.backend_digest} frontend {target.frontend_digest}")
        try:
            prepared = self._prepare(view, attempt)
        except DeployError as exc:
            if self.settings.dry_run:
                log(f"dry run: precheck would fail: {exc.code}")
                return 1
            return self._finish(attempt, "failed", f"precheck:{exc.code}; running release untouched")
        reason = prepared.restore_test_reason
        if self.settings.dry_run:
            self.plan = {
                "revision": target.revision, "backend_image": target.backend_image,
                "frontend_image": target.frontend_image, "ci_run": attempt.ci_run,
                "schema_current": list(prepared.current), "schema_heads": list(prepared.heads),
                "migrations_pending": prepared.pending,
                "stop_bot_before_migration": prepared.pending,
                "backup_before_migration": prepared.pending,
                "restore_test_before_deploy": reason,
                "rollback_target": view.last_success.source_revision if view.last_success else None,
                "blockers": list(self._dry_run_blockers),
            }
            print(json.dumps(self.plan, indent=2, sort_keys=True))
            return 0
        if self.settings.rollback_drill and (prepared.pending or view.last_success is None):
            # A drill must be able to roll back: no schema change, a known previous release.
            log("rollback drill refused: needs no pending migration and a previous deployment")
            return 1
        dr_checkout: Path | None = None
        if prepared.pending or reason is not None:
            try:
                dr_checkout = self._dr_checkout(target.revision)
            except DeployError as exc:
                return self._finish(attempt, "failed", f"dr_checkout_failed:{exc.code}; "
                                    "running release untouched")
        if prepared.pending:
            assert dr_checkout is not None
            failure = self._migration_path(prepared, attempt, dr_checkout, reason or "migration_pending")
            if failure is not None:
                return failure
        elif reason is not None:
            try:
                self._restore_test(target.revision, reason)
            except DeployError as exc:
                return self._finish(attempt, "failed", f"restore_test_failed({reason}):{exc.code}; "
                                    "nothing deployed; running release untouched")
        try:
            self._record_started(attempt)
        except DeployError as exc:
            kept = "; bot stays stopped" if attempt.bot_stopped else "; running release untouched"
            return self._finish(attempt, "failed", f"{exc.code}{kept}")
        try:
            self._compose_up(prepared.release_dir, target, attempt.attempt_id)
            self._verify(target, attempt.attempt_id)
            self._wait_healthy()
            if self.settings.rollback_drill:
                raise DeployError("rollback_drill")
        except DeployError as exc:
            return self._unhealthy(view, attempt, exc)
        warnings = self._after_success(view, target)
        detail = "ok" if not warnings else "ok; warnings=" + ",".join(warnings)
        return self._finish(attempt, "deployed", detail)

    def _migration_path(
        self, prepared: Prepared, attempt: Attempt, dr_checkout: Path, reason: str,
    ) -> int | None:
        """Stop the writer, back up, restore-test, migrate (Will 2026-09-25).

        From the moment the bot is stopped every failure leaves it stopped: the
        way out is a new release (roll forward) or --retry, never the old one.
        """
        stopped = self._stop_bot()
        if stopped == "BOT_STOP_FAILED":
            return self._finish(attempt, "failed", "bot_stop_failed; nothing migrated; "
                                "running release untouched")
        attempt.bot_stopped = True
        held = "bot stopped until a release deploys (roll forward or --retry)"
        try:
            self._backup(dr_checkout)
        except DeployError as exc:
            return self._finish(attempt, "failed", f"backup_failed:{exc.code}; schema unchanged; {held}")
        try:
            self._restore_test(attempt.target.revision, reason)
        except DeployError as exc:
            return self._finish(attempt, "failed", f"restore_test_failed({reason}):{exc.code}; "
                                f"schema unchanged; {held}")
        return self._migrate(prepared, attempt, held)

    def _prepare(self, view: LedgerView, attempt: Attempt) -> Prepared:
        target = attempt.target
        self._fetch_and_require_on_main(target.revision)
        release_dir = self._materialize(target.revision)
        changed_paths, why = self._changed_paths(view, target.revision)
        if changed_paths is None:
            log(f"changed paths unavailable ({why}); treated as a DR change")
        self._validate_env_files(release_dir)
        self._refuse_foreign_containers()
        current, heads = self._schema(target)
        return Prepared(release_dir=release_dir, current=current, heads=heads,
                        changed_paths=changed_paths)

    def _fetch_and_require_on_main(self, revision: str) -> None:
        remote_ref = f"refs/remotes/{self.settings.remote}/{self.settings.branch}"
        self._git("fetch", "--quiet", "--no-tags", self.settings.remote,
                  f"+refs/heads/{self.settings.branch}:{remote_ref}", code="git_fetch_failed",
                  timeout=300.0)
        if self._git_try("cat-file", "-e", f"{revision}^{{commit}}").returncode != 0:
            raise DeployError("revision_not_in_mirror")
        ancestry = self._git_try("merge-base", "--is-ancestor", revision, remote_ref).returncode
        if ancestry == 1:
            raise DeployError("revision_not_on_main")
        if ancestry != 0:
            raise DeployError("git_merge_base_failed")

    def _schema(self, target: Target) -> tuple[tuple[str, ...], tuple[str, ...]]:
        self._pull((target.backend_image, target.frontend_image))
        heads = _alembic_revisions(self._one_shot(target.backend_image, ("heads",), 120.0).stdout)
        current = _alembic_revisions(self._one_shot(target.backend_image, ("current",), 120.0).stdout)
        if not heads:
            raise DeployError("schema_heads_empty")
        if not current:
            raise DeployError("schema_current_empty")
        return current, heads

    def _materialize(self, revision: str) -> Path:
        """Write the compose file and live.env of <revision>; the mirror's worktree is never touched."""
        release_dir = self.settings.state_dir / "releases" / revision
        for path in (COMPOSE_PATH, LIVE_ENV_PATH):
            content = self._git("show", f"{revision}:{path}", code=f"release_file_missing:{path}").stdout
            _write_private(release_dir / Path(path).name, content)
        return release_dir

    def _changed_paths(self, view: LedgerView, revision: str) -> tuple[tuple[str, ...] | None, str]:
        if view.last_success is None:
            return None, "no_previous_deployment"
        previous = view.last_success.source_revision
        if self._git_try("cat-file", "-e", f"{previous}^{{commit}}").returncode != 0:
            return None, "previous_revision_unknown"
        # --no-renames: a rename must report both paths, or moving a file out of
        # a DR path would be judged by its new name alone.
        diff = self._git_try("diff", "--name-only", "--no-renames", "-z", previous, revision,
                             timeout=120.0)
        if diff.returncode != 0:
            return None, "diff_failed"
        return tuple(path for path in diff.stdout.split("\0") if path), ""

    def _validate_env_files(self, release_dir: Path) -> None:
        files = [(self.settings.runtime_dir / name, name != "migrate.env")
                 for name in (*RUNTIME_ENV_FILES.values(), "migrate.env")]
        for path, _ in files:
            self._secret_check(path)
        for path, compose in [*files, (release_dir / "live.env", True)]:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise DeployError(f"env_file_unreadable:{path.name}") from None
            parse_env_file(text, name=path.name, compose=compose)

    def _refuse_foreign_containers(self) -> None:
        result = self._check(
            ["docker", "ps", "--all", "--no-trunc", "--format",
             '{{.Names}}\t{{.Label "com.docker.compose.project"}}'],
            code="docker_ps_failed", timeout=60.0,
        )
        for line in result.stdout.splitlines():
            name, _, project = line.partition("\t")
            if name in CONTAINERS.values() and project != PROJECT:
                # Cutover: containers left by the retired immutable-release launcher
                # must be stopped and renamed by the operator; bfx-deploy never deletes
                # them (docs/runbooks/fresh-host-setup.md).
                if self.settings.dry_run:
                    self._dry_run_blockers.append(f"foreign_container_holds_name:{name}")
                    continue
                raise DeployError(f"foreign_container_holds_name:{name}")

    def _pull(self, images: Sequence[str]) -> None:
        hosts = (self.settings.backend_repository.split("/", 1)[0],
                 self.settings.frontend_repository.split("/", 1)[0])
        with docker_auth(self.settings.state_dir, self._registry_credentials, hosts) as env:
            for image in images:
                self._check(["docker", "pull", "--quiet", image], code="image_pull_failed",
                            timeout=1800.0, env=env or None)

    def _one_shot(self, image: str, alembic_args: Sequence[str], timeout: float) -> CommandResult:
        name = f"bfx-deploy-oneshot-{secrets.token_hex(6)}"
        argv = ["docker", "run", "--rm", "--name", name, "--pull=never", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--user", "1000:1000",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--network", self.settings.network, "--workdir", "/app", "--entrypoint", "",
                "--env-file", str(self.settings.runtime_dir / "migrate.env"),
                "--env", "PYTHONDONTWRITEBYTECODE=1",
                image, "/app/.venv/bin/alembic", *alembic_args]
        try:
            return self._check(argv, code=f"alembic_{alembic_args[0]}_failed", timeout=timeout)
        except CommandError as exc:
            if exc.timed_out:
                self._run(["docker", "rm", "--force", name], timeout=60.0)
            raise

    # ------------------------------------------------------------------ DR checkouts

    def _dr_checkout(self, revision: str) -> Path:
        """A clean checkout of <revision> for its own DR scripts (design review #2).

        A git worktree of the mirror, owned by the mirror user like the mirror
        itself: the drill requires its config to be a clean tracked file and
        finds docker-compose.dr.yml at the repository root, so it needs the whole
        tree, not loose files. Reused when it already sits clean at <revision>.

        git inherits the service's UMask=0077, but the isolated restore's
        postgres (another uid) reads pgbackrest.conf through a bind mount, so
        the tree is made world-readable -- also on reuse, which repairs a
        checkout left 0700 by an earlier run. git tracks only the owner's
        execute bit, so the checkout stays clean.
        """
        path = self.settings.dr_root / revision
        if path.exists():
            head = self._git_try("rev-parse", "HEAD", at=path)
            status = self._git_try("status", "--porcelain", "--untracked-files=no", at=path)
            if head.returncode == 0 and head.stdout.strip() == revision and \
                    status.returncode == 0 and not status.stdout.strip():
                _readable_by_all(path)
                return path
            self._git("worktree", "remove", "--force", str(path), code="dr_checkout_remove_failed")
        self._git("worktree", "add", "--detach", "--force", str(path), revision,
                  code="dr_checkout_add_failed", timeout=300.0)
        _readable_by_all(path)
        return path

    def _backup(self, dr_checkout: Path) -> None:
        script = dr_checkout / BACKUP_SCRIPT
        # backup.sh writes its evidence under ${HOME}/bfx; runuser sets HOME to the
        # backup user's home, exactly as bfx-pgbackrest-backup.service (User=ubuntu).
        self._check(["runuser", "-u", self.settings.backup_user, "--", str(script),
                     "--type", "diff"], code="backup_sh_failed", timeout=7200.0)

    def _restore_test(self, revision: str, reason: str) -> None:
        """Run the isolated restore test with <revision>'s DR scripts and wait.

        bfx-restore-test@<revision>.service (User=ubuntu) runs the ledger-mode
        drill from dr_root/<revision>, refreshes the heartbeat on success and
        alerts on failure itself; `systemctl start` on a oneshot blocks until it
        finishes.
        """
        log(f"isolated restore test with {revision[:12]}'s DR scripts ({reason})")
        self._check(["systemctl", "start", RESTORE_TEST_UNIT.format(revision=revision)],
                    code="restore_test_unit_failed", timeout=7500.0)

    def _migrate(self, prepared: Prepared, attempt: Attempt, held: str) -> int | None:
        target = attempt.target
        upgrade_error: DeployError | None = None
        try:
            self._one_shot(target.backend_image, ("upgrade", "head"), 900.0)
        except DeployError as exc:
            upgrade_error = exc
        try:
            after: tuple[str, ...] | None = _alembic_revisions(
                self._one_shot(target.backend_image, ("current",), 120.0).stdout)
        except DeployError:
            after = None
        if upgrade_error is None and after == prepared.heads:
            attempt.migrations_applied = True
            return None
        if after == prepared.current:
            code = upgrade_error.code if upgrade_error is not None else "head_mismatch"
            return self._finish(attempt, "failed", f"migration_failed:{code}; schema unchanged; {held}")
        attempt.migrations_applied = True
        return self._finish(attempt, "failed", f"migration_partial_or_unverified; schema changed; {held}")

    # ------------------------------------------------------------------ containers

    def _record_started(self, attempt: Attempt) -> None:
        """Append the attempt's `started` row before any container changes.

        The bot reads it at boot (BFX_DEPLOYMENT_ID) as the record of its own
        deployment; a deploy whose started row cannot be written does not start.
        """
        target = attempt.target
        detail = _sanitize(f"{attempt.action}; "
                           f"migrations={'applied' if attempt.migrations_applied else 'none'}")
        try:
            self._ledger.append(LedgerEntry(
                attempt_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=None,
                source_revision=target.revision, backend_digest=target.backend_digest,
                frontend_digest=target.frontend_digest,
                migrations_applied=attempt.migrations_applied, outcome="started", detail=detail,
                ci_run=attempt.ci_run))
        except DeployError as exc:
            raise DeployError(f"ledger_started_unrecorded:{exc.code}") from None
        attempt.started_recorded = True

    def _compose_env(self, target: Target, deployment_id: str) -> dict[str, str]:
        return {**_base_env(), "BFX_BACKEND_IMAGE": target.backend_image,
                "BFX_BACKEND_DIGEST": target.backend_digest,
                "BFX_FRONTEND_IMAGE": target.frontend_image,
                "BFX_FRONTEND_DIGEST": target.frontend_digest,
                "BFX_SOURCE_REVISION": target.revision,
                "BFX_DEPLOYMENT_ID": deployment_id,
                # The env files Compose reads are the ones validated above.
                "BFX_RUNTIME_DIR": str(self.settings.runtime_dir)}

    def _compose_up(self, release_dir: Path, target: Target, deployment_id: str,
                    *, force_recreate: bool = False) -> None:
        for image, digest in ((target.backend_image, target.backend_digest),
                              (target.frontend_image, target.frontend_digest)):
            if DIGEST.fullmatch(digest) is None or not image.endswith("@" + digest):
                raise DeployError("image_reference_not_digest_pinned")
        options = ["--detach", "--no-deps", *(["--force-recreate"] if force_recreate else [])]
        self._check(["docker", "compose", "-p", PROJECT, "-f",
                     str(release_dir / Path(COMPOSE_PATH).name), "up", *options, *SERVICES],
                    code="compose_up_failed", timeout=600.0,
                    env=self._compose_env(target, deployment_id))

    def _verify(self, target: Target, deployment_id: str) -> None:
        result = self._check(["docker", "inspect", "--type", "container", *CONTAINERS.values()],
                             code="docker_inspect_failed", timeout=60.0)
        try:
            records = {str(record["Name"]).lstrip("/"): record for record in json.loads(result.stdout)}
        except (ValueError, KeyError, TypeError):
            raise DeployError("docker_inspect_unparsable") from None
        problems = []
        for service, container in CONTAINERS.items():
            record = records.get(container)
            if record is None:
                problems.append(f"{service}:missing")
                continue
            problems += [f"{service}:{m}" for m in container_mismatches(
                record, service=service, target=target, deployment_id=deployment_id)]
        if problems:
            raise DeployError("container_release_mismatch:" + ",".join(problems))

    def _probe(self, service: str) -> bool:
        if service == "frontend":
            return self._http(self.settings.frontend_url, 5.0) == 200
        port = self.settings.healthz_port if service == "bot" else 8000
        path = "healthz" if service == "bot" else "health"
        result = self._run(["docker", "exec", CONTAINERS[service], "/app/.venv/bin/python", "-c",
                            PROBE, f"http://127.0.0.1:{port}/{path}"], timeout=20.0)
        return result.returncode == 0

    def _restart_counts(self) -> dict[str, str]:
        result = self._check(["docker", "inspect", "--type", "container", "--format",
                              "{{.Name}} {{.RestartCount}} {{.State.Running}}",
                              *CONTAINERS.values()], code="docker_inspect_failed", timeout=60.0)
        counts = {}
        for line in result.stdout.splitlines():
            name, _, rest = line.strip().lstrip("/").partition(" ")
            counts[name] = rest
        return counts

    def _wait_healthy(self) -> None:
        started = self._clock()
        pending = set(SERVICES)
        while pending:
            for service in sorted(pending):
                if self._probe(service):
                    pending.discard(service)
            if not pending:
                break
            elapsed = self._clock() - started
            late = sorted(s for s in pending if elapsed >= self.settings.health_timeouts[s])
            if late:
                raise DeployError("health_timeout:" + ",".join(late))
            self._sleep(self.settings.health_interval)
        # A daemon can answer once and then die in its startup checks: require the
        # same containers, still running, still healthy after a settle window.
        before = self._restart_counts()
        self._sleep(self.settings.settle_seconds)
        after = self._restart_counts()
        if after != before or any(not value.endswith("true") for value in after.values()):
            raise DeployError("restarted_during_settle")
        unhealthy = [service for service in SERVICES if not self._probe(service)]
        if unhealthy:
            raise DeployError("unhealthy_after_settle:" + ",".join(unhealthy))

    def _stop_bot(self) -> str:
        result = self._run(["docker", "stop", "--time", "30", CONTAINERS["bot"]], timeout=90.0)
        if result.returncode == 0:
            return "bot stopped"
        if "no such container" in result.stderr.lower():
            return "bot absent"
        log(f"docker stop bfx-bot failed: {result.stderr.strip()[-500:]}")
        return "BOT_STOP_FAILED"

    def _unhealthy(self, view: LedgerView, attempt: Attempt, exc: DeployError) -> int:
        reason = exc.code
        previous = view.last_success
        if attempt.migrations_applied or attempt.bot_stopped or previous is None:
            why = "migrations applied" if attempt.migrations_applied else "no previous deployment"
            stopped = self._stop_bot()
            return self._finish(attempt, "failed", f"unhealthy:{reason}; {why}; no automatic "
                                f"rollback; {stopped}")
        try:
            self._rollback_to(previous)
        except DeployError as rollback:
            stopped = self._stop_bot()
            return self._finish(attempt, "failed", f"unhealthy:{reason}; rollback_failed:"
                                f"{rollback.code}; {stopped}")
        return self._finish(attempt, "rolled_back", f"unhealthy:{reason}; rolled_back_to="
                            f"{previous.source_revision}")

    def _rollback_to(self, previous: LedgerRow) -> None:
        """Recreate the previous successful release under its own deployment id."""
        log(f"rolling back to {previous.source_revision}")
        target = self._target(previous.source_revision, previous.backend_digest,
                              previous.frontend_digest)
        release_dir = self._materialize(previous.source_revision)
        self._pull((target.backend_image, target.frontend_image))
        self._compose_up(release_dir, target, previous.attempt_id)
        self._verify(target, previous.attempt_id)
        self._wait_healthy()

    # ------------------------------------------------------------------ recreate

    def _recreate(self) -> int:
        """Recreate the running release after a runtime env change.

        Same digests, same revision, schema already at the release's head. No
        rollback exists: the previous env is gone.
        """
        started_at = _now_iso(self._clock)
        try:
            view = self._ledger.read()
        except DeployError as exc:
            log(f"ledger unreadable: {exc.code}")
            return 1
        previous = view.last_success
        if previous is None:
            log("recreate refused: no successful deployment to recreate")
            return 1
        attempt = Attempt(
            started_at=started_at, action="recreate", ci_run=previous.ci_run,
            target=self._target(previous.source_revision, previous.backend_digest,
                                previous.frontend_digest),
        )
        target = attempt.target
        try:
            if self._git_try("cat-file", "-e", f"{target.revision}^{{commit}}").returncode != 0:
                raise DeployError("revision_not_in_mirror")
            release_dir = self._materialize(target.revision)
            self._validate_env_files(release_dir)
            self._refuse_foreign_containers()
            current, heads = self._schema(target)
            if set(current) != set(heads):
                raise DeployError("schema_not_at_release_head")
        except DeployError as exc:
            if self.settings.dry_run:
                log(f"dry run: recreate precheck would fail: {exc.code}")
                return 1
            return self._finish(attempt, "failed", f"precheck:{exc.code}; running release untouched")
        if self.settings.dry_run:
            self.plan = {"action": "recreate", "revision": target.revision,
                         "backend_image": target.backend_image, "frontend_image": target.frontend_image,
                         "blockers": list(self._dry_run_blockers)}
            print(json.dumps(self.plan, indent=2, sort_keys=True))
            return 0
        try:
            self._record_started(attempt)
        except DeployError as exc:
            return self._finish(attempt, "failed", f"{exc.code}; running release untouched")
        try:
            self._compose_up(release_dir, target, attempt.attempt_id, force_recreate=True)
            self._verify(target, attempt.attempt_id)
            self._wait_healthy()
        except DeployError as exc:
            stopped = self._stop_bot()
            return self._finish(attempt, "failed", f"unhealthy:{exc.code}; recreate has no rollback; "
                                f"{stopped}; fix the runtime env and --recreate")
        return self._finish(attempt, "deployed", "ok")

    # ------------------------------------------------------------------ after success

    def _after_success(self, view: LedgerView, target: Target) -> list[str]:
        warnings = []
        if self.settings.local_alias:
            # Transitional: the DR verifier (docker-compose.dr.yml) still names
            # bfx-bot:local; keep that alias on the running backend so it never
            # drifts behind production. Never used to deploy. (The weekly report
            # runs the ledger's backend digest: bfx_weekly_report.py.)
            tagged = self._run(["docker", "tag", target.backend_image, self.settings.local_alias],
                               timeout=60.0)
            if tagged.returncode != 0:
                warnings.append("local_alias_failed")
        previous = view.last_success.source_revision if view.last_success else None
        try:
            self._install_tooling(target.revision)
        except (DeployError, OSError) as exc:
            code = exc.code if isinstance(exc, DeployError) else type(exc).__name__
            log(f"tooling install failed: {code}")
            warnings.append(f"tooling_install_failed:{code}")
        else:
            warnings += self._prune_releases(keep={target.revision, *([previous] if previous else [])})
        keep = {target.backend_digest, target.frontend_digest}
        if view.last_success is not None:
            keep |= {view.last_success.backend_digest, view.last_success.frontend_digest}
        for repository in (self.settings.backend_repository, self.settings.frontend_repository):
            listed = self._run(["docker", "image", "ls", "--digests", "--no-trunc", "--format",
                                "{{.Digest}}", repository], timeout=60.0)
            if listed.returncode != 0:
                warnings.append("image_prune_list_failed")
                continue
            for digest in sorted(set(listed.stdout.split())):
                if DIGEST.fullmatch(digest) and digest not in keep:
                    removed = self._run(["docker", "image", "rm", f"{repository}@{digest}"],
                                        timeout=120.0)
                    if removed.returncode != 0:
                        warnings.append("image_prune_failed")
        return sorted(set(warnings))

    def _install_tooling(self, revision: str) -> None:
        """Install <revision>'s host tooling and units; effective from the next run.

        The deployer that started this run finishes it: it never switches code
        mid-deploy, and tooling from a release that did not deploy is never
        installed. Files come from git objects (as the compose file does), are
        written root-owned under ops_root/releases/<rev>, get their own uv
        environment from the release's uv.lock, and only then become `current`.
        Unit files are installed and reloaded, never enabled, started or stopped.
        The DR checkout `current` (scheduled backups, status, monthly restore
        test) moves to <revision> at the same point.
        """
        releases = self.settings.ops_root / "releases"
        release = releases / revision
        if not (release / ".installed").is_file():
            releases.mkdir(mode=0o755, parents=True, exist_ok=True)
            stage = releases / f".{revision}.staging"
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(release, ignore_errors=True)
            listed = self._git("ls-tree", "-r", "-z", "--name-only", revision, "--", *TOOLING_PATHS,
                               code="tooling_list_failed")
            paths = [p for p in listed.stdout.split("\0") if p and "__pycache__" not in p]
            if not any(p == "deploy/vm/ops/uv.lock" for p in paths):
                raise DeployError("tooling_lock_missing")
            for path in paths:
                content = self._git("show", f"{revision}:{path}", code="tooling_file_missing").stdout
                destination = stage / path.removeprefix("deploy/vm/")
                destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                _write_atomic(destination, content, 0o755 if path.endswith(".sh") else 0o644)
            ops = stage / "ops"
            self._check([self.settings.uv, "sync", "--frozen", "--no-install-project",
                         "--python", self.settings.system_python, "--project", str(ops)],
                        code="tooling_uv_sync_failed", timeout=600.0,
                        env={**_base_env(), "UV_PROJECT_ENVIRONMENT": str(ops / ".venv"),
                             "UV_CACHE_DIR": str(self.settings.state_dir / "uv-cache"),
                             "UV_PYTHON_DOWNLOADS": "never"})
            _readable_by_all(stage)
            _write_atomic(stage / ".installed", revision + "\n", 0o644)
            os.replace(stage, release)
        for name in managed_units(release / "systemd" / "managed-units"):
            source = release / "systemd" / name
            _write_atomic(self.settings.unit_dir / name, source.read_text(encoding="utf-8"), 0o644)
        _point_symlink(self.settings.ops_root / "current", f"releases/{revision}")
        self._check(["systemctl", "daemon-reload"], code="systemd_reload_failed", timeout=60.0)
        checkout = self._dr_checkout(revision)
        _point_symlink(self.settings.dr_root / "current", checkout.name)

    def _prune_releases(self, keep: set[str]) -> list[str]:
        """Drop tooling releases and DR checkouts other than the deployed and previous ones."""
        warnings = []
        releases = self.settings.ops_root / "releases"
        for entry in sorted(releases.iterdir()) if releases.is_dir() else []:
            if entry.name not in keep:
                shutil.rmtree(entry, ignore_errors=True)
        root = self.settings.dr_root
        for entry in sorted(root.iterdir()) if root.is_dir() else []:
            if entry.is_symlink() or not entry.is_dir() or entry.name in keep \
                    or REVISION.fullmatch(entry.name) is None:
                continue
            removed = self._git_try("worktree", "remove", "--force", str(entry), timeout=120.0)
            if removed.returncode != 0:
                warnings.append("dr_checkout_prune_failed")
        self._git_try("worktree", "prune")
        return warnings

    # ------------------------------------------------------------------ ledger

    def _finish(self, attempt: Attempt, outcome: str, detail: str) -> int:
        target = attempt.target
        prefix = "recreate; " if attempt.action == "recreate" else ""
        text = _sanitize(f"{prefix}{detail}")
        entry = LedgerEntry(
            attempt_id=attempt.attempt_id, started_at=attempt.started_at,
            finished_at=_now_iso(self._clock),
            source_revision=target.revision, backend_digest=target.backend_digest,
            frontend_digest=target.frontend_digest,
            migrations_applied=attempt.migrations_applied, outcome=outcome, detail=text,
            ci_run=attempt.ci_run,
        )
        level = {"deployed": "info", "rolled_back": "warning"}.get(outcome, "critical")
        if outcome == "deployed" and "warnings=" in detail:
            level = "warning"
        recorded = True
        try:
            ledger_note = f"ledger #{self._ledger.append(entry)}"
        except DeployError as exc:
            recorded, level = False, "critical"
            ledger_note = f"LEDGER WRITE FAILED ({exc.code})"
        message = (f"{attempt.action} {outcome}: {target.revision[:12]} "
                   f"migrations={'applied' if attempt.migrations_applied else 'none'}; {detail}; "
                   f"{ledger_note}")
        log(message)
        self._notify(level, message)
        return 0 if outcome == "deployed" and recorded else 1


def _base_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
           "HOME": os.environ.get("HOME") or "/root"}
    if os.environ.get("DOCKER_HOST"):
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return env


def load_registry_credentials(path: Path, secret_check: Callable[[Path], None]) -> tuple[str, str] | None:
    """GHCR_USERNAME / GHCR_TOKEN from a root 0600 env file; absent file means anonymous."""
    if not path.exists() and not path.is_symlink():
        return None
    secret_check(path)
    values = parse_env_file(path.read_text(encoding="utf-8"), name=path.name, compose=False)
    username, token = values.get("GHCR_USERNAME", ""), values.get("GHCR_TOKEN", "")
    if not username or not token:
        raise DeployError(f"registry_credentials_incomplete:{path.name}")
    return username, token


def _parser() -> argparse.ArgumentParser:
    defaults = Settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mirror", type=Path, default=defaults.mirror,
                        help="git clone of the repository with a read-only deploy key")
    parser.add_argument("--mirror-user", help="run git as this user (default: mirror owner)")
    parser.add_argument("--remote", default=defaults.remote)
    parser.add_argument("--branch", default=defaults.branch)
    parser.add_argument("--runtime-dir", type=Path, default=defaults.runtime_dir)
    parser.add_argument("--state-dir", type=Path, default=defaults.state_dir)
    parser.add_argument("--lock-file", type=Path, default=defaults.lock_file)
    parser.add_argument("--backend-repository", default=defaults.backend_repository)
    parser.add_argument("--frontend-repository", default=defaults.frontend_repository)
    parser.add_argument("--network", default=defaults.network)
    parser.add_argument("--postgres-container", default=defaults.postgres_container)
    parser.add_argument("--db-user", default=defaults.db_user)
    parser.add_argument("--db-name", default=defaults.db_name)
    parser.add_argument("--dr-root", type=Path, default=defaults.dr_root)
    parser.add_argument("--ops-root", type=Path, default=defaults.ops_root)
    parser.add_argument("--uv", default=defaults.uv)
    parser.add_argument("--backup-user", help="run backup.sh as this user (default: mirror owner)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="discover, fetch, diff, pull and compare schema; change nothing")
    mode.add_argument("--rollback-drill", action="store_true",
                      help="deploy the new release, then take the real rollback path on purpose "
                           "(refused when migrations are pending); run with the timer stopped, "
                           "then deploy it for real with --retry")
    parser.add_argument("--retry", action="store_true",
                        help="attempt a digest whose last attempt failed, rolled back or was interrupted")
    parser.add_argument("--recreate", action="store_true",
                        help="recreate the running release (same digests) after a runtime env change; "
                             "combine with --dry-run to preview")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.recreate and (args.retry or args.rollback_drill):
        log("--recreate cannot be combined with --retry or --rollback-drill")
        return 2
    if os.geteuid() != 0:
        log("must run as root (reads /opt/bfx/runtime and drives docker)")
        return 2
    mirror: Path = args.mirror
    try:
        owner = pwd.getpwuid(mirror.stat().st_uid).pw_name
    except (OSError, KeyError):
        log(f"mirror {mirror} is not readable")
        return 2
    settings = Settings(
        mirror=mirror, mirror_user=args.mirror_user or owner, remote=args.remote,
        branch=args.branch, runtime_dir=args.runtime_dir, state_dir=args.state_dir,
        lock_file=args.lock_file, backend_repository=args.backend_repository,
        frontend_repository=args.frontend_repository, network=args.network,
        postgres_container=args.postgres_container, db_user=args.db_user, db_name=args.db_name,
        dr_root=args.dr_root, ops_root=args.ops_root, uv=args.uv,
        backup_user=args.backup_user or owner, dry_run=args.dry_run, retry=args.retry,
        rollback_drill=args.rollback_drill,
        recreate=args.recreate,
    )
    notify_config = settings.runtime_dir / "notify.env"

    def notify(level: str, text: str) -> None:
        bfx_notify.send(text, level=level, config_path=notify_config)

    try:
        credentials = load_registry_credentials(settings.runtime_dir / "ghcr.env", protected_secret_file)
    except (DeployError, OSError) as exc:
        code = exc.code if isinstance(exc, DeployError) else type(exc).__name__
        log(f"registry credentials unusable: {code}")
        if not settings.dry_run:
            notify("critical", f"bfx-deploy cannot use registry credentials ({code})")
        return 1
    hosts = (settings.backend_repository.split("/", 1)[0], settings.frontend_repository.split("/", 1)[0])
    deployer = Deployer(
        settings, runner=subprocess_runner,
        registry=ImagetoolsRegistry(
            subprocess_runner, lambda: docker_auth(settings.state_dir, credentials, hosts)),
        ledger=PsqlLedger(subprocess_runner, container=settings.postgres_container,
                          db_user=settings.db_user, db_name=settings.db_name),
        notify=notify, registry_credentials=credentials,
    )
    return deployer.run()


if __name__ == "__main__":
    raise SystemExit(main())
