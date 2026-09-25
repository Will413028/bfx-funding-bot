#!/usr/bin/env python3
"""Deploy the newest green `main` release to the VM, by digest only.

Runs as root from bfx-deploy.timer (every 5 minutes) or by hand. Standard
library only: the VM runs Ubuntu 24.04's python3.12 and has no venv for host
tooling. ADRs: 2026-09-25-ci-registry-digest-deploy (D1-D5) and
2026-09-25-automated-probation-replaces-release-ceremony (D1 change class,
D6 backup before migration).

One run:
  1. flock, so two runs never overlap;
  2. resolve `backend:main` on GHCR to a digest; stop if the ledger's newest
     successful deployment already runs it (or its newest attempt already tried
     it and failed -- a failed digest is retried only with --retry);
  3. read the revision label from that image, require `backend:sha-<rev>` to be the
     same digest, and resolve `frontend:sha-<rev>`;
  4. fetch the VM mirror, require <rev> on origin/main, and take the compose file,
     live.env and change-class rules from <rev> itself;
  5. classify `git diff --name-only --no-renames <deployed>..<rev>` (default
     material; undecidable is material; --force-material can only raise it);
  6. pull both images by digest, validate the env files, compare
     `alembic current` with `alembic heads` in a one-shot of the new image;
  7. if migrations are pending: backup.sh --type diff must succeed first, then
     `alembic upgrade head`;
  8. `docker compose -p bfx-app up -d --no-deps bot webapi frontend`, re-inspect
     every container's hardening, wait for health (bot healthz, webapi health,
     frontend 127.0.0.1:3001) and a settle window;
  9. on failure without applied migrations: recreate the previous successful
     digests (rolled_back). With applied migrations, or nothing to roll back to:
     stop the bot, never roll back (old code may not run on the new schema);
 10. append the attempt to the append-only `deployments` ledger and alert.

Every external command goes through an injectable runner; a failing command is
never ignored -- it either fails the attempt or is reported as a warning.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
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
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


change_class = _load("_bfx_ops_change_class", _HERE / "change_class.py")
bfx_notify = _load("_bfx_ops_notify", _HERE / "bfx_notify.py")

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
REPOSITORY = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+")
ALEMBIC_REVISION = re.compile(r"[A-Za-z0-9_]{1,64}")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
REVISION_LABEL = "org.opencontainers.image.revision"
PROJECT = "bfx-app"
COMPOSE_PATH = "deploy/vm/docker-compose.app.yml"
LIVE_ENV_PATH = "deploy/vm/live.env"
SERVICES = ("bot", "webapi", "frontend")
CONTAINERS = {"bot": "bfx-bot", "webapi": "bfx-webapi", "frontend": "bfx-frontend"}
RUNTIME_ENV_FILES = {"bot": "bot.env", "webapi": "webapi.env", "frontend": "frontend.env"}
FORBIDDEN_ENV = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "NODE_OPTIONS",
)
RESERVED_ENV = ("BFX_IMAGE_DIGEST", "BFX_SOURCE_REVISION", "BFX_CHANGE_CLASS")
BACKEND_COMMANDS = {
    "bot": ("/app/.venv/bin/python", "-m", "bfx_funding_bot.modules.marketfeed.daemon"),
    "webapi": ("/app/.venv/bin/python", "-m", "uvicorn", "bfx_funding_bot.main:app",
               "--host", "0.0.0.0", "--port", "8000"),
}
FRONTEND_COMMAND = ("node", "server.js")
FRONTEND_PORTS = {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3001"}]}
PROBE = (
    "import sys,urllib.request;"
    "sys.exit(0 if urllib.request.urlopen(sys.argv[1],timeout=5).status==200 else 1)"
)
MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_MAX_REGISTRY_BODY = 4 * 1024 * 1024
_MAX_DETAIL = 1800


def log(message: str) -> None:
    print(f"bfx-deploy: {message}", file=sys.stderr, flush=True)


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
    """Strict KEY=VALUE parser (the old release_package.read_env contract).

    With compose=True, values Compose would read differently from `docker
    --env-file` (quotes, `$` interpolation, ` #` inline comments, padding) are
    rejected instead of silently changing a secret on the first Compose deploy.
    Error codes name the file and key, never the value.
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
        if key in FORBIDDEN_ENV or key in RESERVED_ENV:
            raise DeployError(f"env_file_forbidden_key:{name}:{key}")
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
    """Root-controlled secret: regular 0600 file, no symlink, no writable ancestor."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise DeployError(f"secret_file_missing:{path.name}")
    info = path.lstat()
    if info.st_uid != owner_uid:
        raise DeployError(f"secret_file_not_root_controlled:{path.name}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise DeployError(f"secret_file_mode_not_0600:{path.name}")
    for entry in path.parents:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
            raise DeployError(f"secret_file_not_root_controlled:{path.name}")


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


# (url, headers, credential headers that must not follow redirects, timeout)
Transport = Callable[[str, Mapping[str, str], Mapping[str, str], float], HttpResponse]


def urllib_transport(
    url: str, headers: Mapping[str, str], credentials: Mapping[str, str], timeout: float,
) -> HttpResponse:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    for key, value in credentials.items():
        # Blob downloads redirect to a CDN; the registry token must not follow.
        request.add_unredirected_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_REGISTRY_BODY + 1)
            if len(body) > _MAX_REGISTRY_BODY:
                raise DiscoveryError("registry_response_too_large")
            return HttpResponse(
                int(response.status), {k.lower(): v for k, v in response.headers.items()}, body
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            int(exc.code), {k.lower(): v for k, v in exc.headers.items()}, exc.read(65536)
        )
    except (urllib.error.URLError, OSError) as exc:
        raise DiscoveryError(f"registry_unreachable:{type(exc).__name__}") from None


class RegistryClient:
    """Minimal OCI distribution client: resolve tags, read the revision label.

    Every manifest and blob is verified against its sha256 digest, so a digest
    returned here is exactly the content that will be pulled.
    """

    def __init__(
        self, *, credentials: tuple[str, str] | None,
        transport: Transport = urllib_transport, platform: tuple[str, str] = ("linux", "arm64"),
        timeout: float = 30.0,
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._platform = platform
        self._timeout = timeout
        self._tokens: dict[str, str] = {}

    def resolve(self, repository: str, tag: str) -> str:
        if TAG.fullmatch(tag) is None:
            raise DiscoveryError("registry_tag_invalid")
        response = self._get(repository, f"manifests/{tag}", MANIFEST_ACCEPT)
        digest = "sha256:" + hashlib.sha256(response.body).hexdigest()
        announced = response.headers.get("docker-content-digest")
        if announced is not None and announced != digest:
            raise DiscoveryError("registry_digest_mismatch")
        return digest

    def revision(self, repository: str, digest: str) -> str:
        manifest = self._json(repository, f"manifests/{digest}", digest, MANIFEST_ACCEPT)
        if isinstance(manifest.get("manifests"), list):
            wanted_os, wanted_arch = self._platform
            children = [
                entry for entry in manifest["manifests"]
                if isinstance(entry, dict)
                and isinstance(entry.get("platform"), dict)
                and entry["platform"].get("os") == wanted_os
                and entry["platform"].get("architecture") == wanted_arch
                and (entry.get("annotations") or {}).get("vnd.docker.reference.type")
                != "attestation-manifest"
            ]
            if len(children) != 1 or DIGEST.fullmatch(str(children[0].get("digest"))) is None:
                raise DiscoveryError("registry_platform_manifest_missing")
            child = str(children[0]["digest"])
            manifest = self._json(repository, f"manifests/{child}", child, MANIFEST_ACCEPT)
        config = manifest.get("config")
        config_digest = config.get("digest") if isinstance(config, dict) else None
        if not isinstance(config_digest, str) or DIGEST.fullmatch(config_digest) is None:
            raise DiscoveryError("registry_config_missing")
        image = self._json(repository, f"blobs/{config_digest}", config_digest, "*/*")
        labels = (image.get("config") or {}).get("Labels") or {}
        revision = labels.get(REVISION_LABEL) if isinstance(labels, dict) else None
        if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
            raise DiscoveryError("registry_revision_label_missing")
        return revision

    def _json(self, repository: str, path: str, digest: str, accept: str) -> dict[str, Any]:
        response = self._get(repository, path, accept)
        if "sha256:" + hashlib.sha256(response.body).hexdigest() != digest:
            raise DiscoveryError("registry_content_digest_mismatch")
        try:
            value = json.loads(response.body)
        except ValueError:
            raise DiscoveryError("registry_json_invalid") from None
        if not isinstance(value, dict):
            raise DiscoveryError("registry_json_invalid")
        return value

    def _get(self, repository: str, path: str, accept: str) -> HttpResponse:
        if REPOSITORY.fullmatch(repository) is None:
            raise DiscoveryError("registry_repository_invalid")
        host, name = repository.split("/", 1)
        url = f"https://{host}/v2/{name}/{path}"
        headers = {"Accept": accept}
        token = self._tokens.get(repository)
        credentials = {"Authorization": f"Bearer {token}"} if token else {}
        response = self._transport(url, headers, credentials, self._timeout)
        if response.status == 401 and token is None:
            token = self._token(host, name, response.headers.get("www-authenticate", ""))
            self._tokens[repository] = token
            response = self._transport(
                url, headers, {"Authorization": f"Bearer {token}"}, self._timeout
            )
        if response.status == 404:
            raise DiscoveryError(f"registry_not_found:{name}:{path.split('/', 1)[1][:80]}")
        if response.status != 200:
            raise DiscoveryError(f"registry_http_{response.status}")
        return response

    def _token(self, host: str, name: str, challenge: str) -> str:
        scheme, _, rest = challenge.partition(" ")
        params = dict(re.findall(r'([A-Za-z]+)="([^"]*)"', rest))
        realm = urllib.parse.urlsplit(params.get("realm", ""))
        # Credentials go only to the registry's own https token endpoint.
        if scheme.lower() != "bearer" or realm.scheme != "https" or realm.hostname != host.split(":")[0]:
            raise DiscoveryError("registry_auth_challenge_rejected")
        query = urllib.parse.urlencode({
            "service": params.get("service", host),
            "scope": params.get("scope") or f"repository:{name}:pull",
        })
        credentials: dict[str, str] = {}
        if self._credentials is not None:
            basic = base64.b64encode(":".join(self._credentials).encode()).decode()
            credentials["Authorization"] = f"Basic {basic}"
        response = self._transport(
            f"{params['realm']}?{query}", {"Accept": "application/json"}, credentials, self._timeout
        )
        if response.status != 200:
            raise DiscoveryError(f"registry_token_http_{response.status}")
        try:
            payload = json.loads(response.body)
        except ValueError:
            raise DiscoveryError("registry_token_invalid") from None
        token = payload.get("token") or payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise DiscoveryError("registry_token_invalid")
        return token


class Registry(Protocol):
    def resolve(self, repository: str, tag: str) -> str: ...
    def revision(self, repository: str, digest: str) -> str: ...


# --------------------------------------------------------------------------- ledger


@dataclass(frozen=True, slots=True)
class LedgerRow:
    id: int
    source_revision: str
    backend_digest: str
    frontend_digest: str
    change_class: str
    outcome: str
    migrations_applied: bool


@dataclass(frozen=True, slots=True)
class LedgerView:
    exists: bool
    last_attempt: LedgerRow | None
    last_success: LedgerRow | None


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    started_at: str
    finished_at: str
    source_revision: str
    backend_digest: str
    frontend_digest: str
    change_class: str
    migrations_applied: bool
    outcome: str
    detail: str
    ci_run: str | None = None


class Ledger(Protocol):
    def read(self) -> LedgerView: ...
    def append(self, entry: LedgerEntry) -> int: ...


_LEDGER_COLUMNS = (
    "id, source_revision, backend_digest, frontend_digest, change_class, outcome, "
    "migrations_applied"
)
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
  started_at, finished_at, source_revision, backend_digest, frontend_digest,
  change_class, migrations_applied, outcome, detail, ci_run)
VALUES (:'started_at', :'finished_at', :'source_revision', :'backend_digest',
  :'frontend_digest', :'change_class', :'migrations_applied', :'outcome', :'detail',
  NULLIF(:'ci_run', ''))
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
            "started_at": entry.started_at, "finished_at": entry.finished_at,
            "source_revision": entry.source_revision, "backend_digest": entry.backend_digest,
            "frontend_digest": entry.frontend_digest, "change_class": entry.change_class,
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
    return LedgerRow(
        id=int(value["id"]), source_revision=str(value["source_revision"]),
        backend_digest=str(value["backend_digest"]), frontend_digest=str(value["frontend_digest"]),
        change_class=str(value["change_class"]), outcome=str(value["outcome"]),
        migrations_applied=bool(value["migrations_applied"]),
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
    pgbackrest_dir: Path = Path("/home/ubuntu/bfx-funding-bot/deploy/vm/pgbackrest")
    backup_user: str = "ubuntu"
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
    force_material: bool = False
    rollback_drill: bool = False


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
    classification: Any
    current: tuple[str, ...]
    heads: tuple[str, ...]

    @property
    def pending(self) -> bool:
        return set(self.current) != set(self.heads)


@dataclass(slots=True)
class Attempt:
    started_at: str
    target: Target
    klass: str = "material"  # until classified, an attempt is material
    class_detail: str = "unclassified"
    migrations_applied: bool = False


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


def container_violations(
    record: Mapping[str, Any], *, service: str, image: str, digest: str,
    revision: str, klass: str, network: str,
) -> list[str]:
    """Hardening the old launcher enforced, re-checked on what Compose created."""
    config = record.get("Config") or {}
    host = record.get("HostConfig") or {}
    networks = (record.get("NetworkSettings") or {}).get("Networks") or {}
    env: dict[str, str] = {}
    for item in config.get("Env") or []:
        key, _, value = str(item).partition("=")
        env[key] = value
    python = service in BACKEND_COMMANDS
    expected_command = list(BACKEND_COMMANDS[service] if python else FRONTEND_COMMAND)
    violations = []
    if record.get("Name") != "/" + CONTAINERS[service]:
        violations.append("name")
    if config.get("Image") != image:
        violations.append("image")
    if config.get("User") != ("1000:1000" if python else "nextjs"):
        violations.append("user")
    if config.get("WorkingDir") != "/app":
        violations.append("workdir")
    if config.get("Cmd") != expected_command:
        violations.append("command")
    if config.get("Entrypoint"):
        violations.append("entrypoint")
    if host.get("ReadonlyRootfs") is not True:
        violations.append("read_only")
    if host.get("Privileged") is not False:
        violations.append("privileged")
    if host.get("CapAdd"):
        violations.append("cap_add")
    if "ALL" not in (host.get("CapDrop") or []):
        violations.append("cap_drop")
    if not {"no-new-privileges", "no-new-privileges:true"} & set(host.get("SecurityOpt") or []):
        violations.append("no_new_privileges")
    if set(networks) != {network}:
        violations.append("network")
    if service == "bot":
        attached = networks.get(network) or {}
        names = set(attached.get("Aliases") or []) | set(attached.get("DNSNames") or [])
        if "bfx-bot" not in names:
            violations.append("alias")
    if record.get("Mounts"):
        violations.append("mounts")
    if (host.get("PortBindings") or {}) != ({} if python else FRONTEND_PORTS):
        violations.append("ports")
    if (host.get("RestartPolicy") or {}).get("Name") != "unless-stopped":
        violations.append("restart")
    if any(env.get(key) for key in FORBIDDEN_ENV):
        violations.append("injection_env")
    if python and env.get("PYTHONDONTWRITEBYTECODE") != "1":
        violations.append("bytecode_env")
    if (env.get("BFX_IMAGE_DIGEST"), env.get("BFX_SOURCE_REVISION"), env.get("BFX_CHANGE_CLASS")) != (
        digest, revision, klass,
    ):
        violations.append("identity_env")
    return violations


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

    def _git_argv(self, *args: str) -> list[str]:
        # Run as the mirror's owner: its deploy key and ~/.ssh live in that user's
        # home, and root-owned objects would break the owner's own git later.
        # runuser sets HOME to the target user's home; nothing relies on root's.
        return ["runuser", "-u", self.settings.mirror_user, "--",
                "git", "-C", str(self.settings.mirror), *args]

    def _git(self, *args: str, code: str, timeout: float = 120.0) -> CommandResult:
        return self._check(self._git_argv(*args), code=code, timeout=timeout)

    def _git_try(self, *args: str, timeout: float = 60.0) -> CommandResult:
        """For git calls whose non-zero exit is an answer, not an error."""
        return self._run(self._git_argv(*args), timeout=timeout)

    # ------------------------------------------------------------------ entry

    def run(self) -> int:
        with self._locked() as acquired:
            if not acquired:
                log("another bfx-deploy run holds the lock; nothing to do")
                return 0
            try:
                return self._run_locked()
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
            backend_digest = self._registry.resolve(settings.backend_repository, settings.branch)
            view = self._ledger.read()
        except DeployError as exc:
            return self._discovery_failed(exc)
        if view.last_success is not None and view.last_success.backend_digest == backend_digest:
            self._discovery_ok()
            log(f"up to date: {view.last_success.source_revision} ({backend_digest})")
            return 0
        attempt = view.last_attempt
        if attempt is not None and attempt.backend_digest == backend_digest and not settings.retry:
            self._discovery_ok()
            log(f"{backend_digest} was already attempted (ledger #{attempt.id}, {attempt.outcome}); "
                "waiting for a new release or --retry")
            return 0
        try:
            target = self._discover(backend_digest)
        except DeployError as exc:
            return self._discovery_failed(exc)
        self._discovery_ok()
        return self._deploy(view, Attempt(started_at=started_at, target=target))

    def _discover(self, backend_digest: str) -> Target:
        settings = self.settings
        revision = self._registry.revision(settings.backend_repository, backend_digest)
        if self._registry.resolve(settings.backend_repository, f"sha-{revision}") != backend_digest:
            raise DiscoveryError("registry_main_and_revision_tag_disagree")
        frontend_digest = self._registry.resolve(settings.frontend_repository, f"sha-{revision}")
        if self._registry.revision(settings.frontend_repository, frontend_digest) != revision:
            raise DiscoveryError("registry_frontend_revision_mismatch")
        return Target(
            revision=revision, backend_digest=backend_digest, frontend_digest=frontend_digest,
            backend_image=f"{settings.backend_repository}@{backend_digest}",
            frontend_image=f"{settings.frontend_repository}@{frontend_digest}",
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
        if self.settings.dry_run:
            self.plan = {
                "revision": target.revision, "backend_image": target.backend_image,
                "frontend_image": target.frontend_image, "change_class": attempt.klass,
                "class_detail": attempt.class_detail, "schema_current": list(prepared.current),
                "schema_heads": list(prepared.heads), "migrations_pending": prepared.pending,
                "backup_before_migration": prepared.pending,
                "rollback_target": view.last_success.source_revision if view.last_success else None,
                "blockers": list(self._dry_run_blockers),
            }
            print(json.dumps(self.plan, indent=2, sort_keys=True))
            return 0
        if self.settings.rollback_drill and (prepared.pending or view.last_success is None):
            # A drill must be able to roll back: no schema change, a known previous release.
            log("rollback drill refused: needs no pending migration and a previous deployment")
            return 1
        if prepared.pending:
            try:
                self._backup()
            except DeployError as exc:
                return self._finish(attempt, "failed", f"backup_failed:{exc.code}; migrations not "
                                    "applied; running release untouched")
            failure = self._migrate(prepared, attempt)
            if failure is not None:
                return failure
        try:
            self._compose_up(prepared.release_dir, target, attempt.klass)
            self._verify(target, attempt.klass)
            self._wait_healthy()
            if self.settings.rollback_drill:
                raise DeployError("rollback_drill")
        except DeployError as exc:
            return self._unhealthy(view, attempt, exc)
        warnings = self._after_success(view, target)
        detail = "ok" if not warnings else "ok; warnings=" + ",".join(warnings)
        return self._finish(attempt, "deployed", detail)

    def _prepare(self, view: LedgerView, attempt: Attempt) -> Prepared:
        target = attempt.target
        remote_ref = f"refs/remotes/{self.settings.remote}/{self.settings.branch}"
        self._git("fetch", "--quiet", "--no-tags", self.settings.remote,
                  f"+refs/heads/{self.settings.branch}:{remote_ref}", code="git_fetch_failed",
                  timeout=300.0)
        if self._git_try("cat-file", "-e", f"{target.revision}^{{commit}}").returncode != 0:
            raise DeployError("revision_not_in_mirror")
        ancestry = self._git_try("merge-base", "--is-ancestor", target.revision,
                                 remote_ref).returncode
        if ancestry == 1:
            raise DeployError("revision_not_on_main")
        if ancestry != 0:
            raise DeployError("git_merge_base_failed")
        release_dir = self._materialize(target.revision)
        classification = self._classify(view, target.revision)
        attempt.klass = classification.change_class
        attempt.class_detail = classification.describe()
        log(f"change class {attempt.klass} ({attempt.class_detail})")
        self._validate_env_files(release_dir)
        self._refuse_foreign_containers()
        self._pull((target.backend_image, target.frontend_image))
        heads = _alembic_revisions(self._one_shot(target.backend_image, ("heads",), 120.0).stdout)
        current = _alembic_revisions(self._one_shot(target.backend_image, ("current",), 120.0).stdout)
        if not heads:
            raise DeployError("schema_heads_empty")
        if not current:
            raise DeployError("schema_current_empty")
        return Prepared(release_dir=release_dir, classification=classification,
                        current=current, heads=heads)

    def _materialize(self, revision: str) -> Path:
        """Write the compose file and live.env of <revision>; the mirror's worktree is never touched."""
        release_dir = self.settings.state_dir / "releases" / revision
        for path in (COMPOSE_PATH, LIVE_ENV_PATH):
            content = self._git("show", f"{revision}:{path}", code=f"release_file_missing:{path}").stdout
            _write_private(release_dir / Path(path).name, content)
        return release_dir

    def _classify(self, view: LedgerView, revision: str) -> Any:
        result = self._classify_paths(view, revision)
        if self.settings.force_material:
            result = change_class.raise_to_material(result, "operator_forced")
        return result

    def _classify_paths(self, view: LedgerView, revision: str) -> Any:
        if view.last_success is None:
            return change_class.undecidable("no_previous_deployment")
        previous = view.last_success.source_revision
        if self._git_try("cat-file", "-e", f"{previous}^{{commit}}").returncode != 0:
            return change_class.undecidable("previous_revision_unknown")
        rules = self._git_try("show", f"{revision}:{change_class.RULES_PATH}")
        if rules.returncode != 0:
            return change_class.undecidable("rules_unavailable")
        try:
            parsed = change_class.parse_rules(rules.stdout)
        except change_class.RulesError as exc:
            log(f"change-class rules invalid at {revision}: {exc}")
            return change_class.undecidable("rules_invalid")
        # --no-renames: a rename must report both paths, or moving a file out of
        # backend/src into docs/ would be judged by its new name alone.
        diff = self._git_try("diff", "--name-only", "--no-renames", "-z", previous, revision,
                             timeout=120.0)
        if diff.returncode != 0:
            return change_class.undecidable("diff_failed")
        return change_class.classify([path for path in diff.stdout.split("\0") if path], parsed)

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
                # Cutover: the old immutable-release containers must be stopped and
                # renamed by the operator; bfx-deploy never deletes them.
                if self.settings.dry_run:
                    self._dry_run_blockers.append(f"foreign_container_holds_name:{name}")
                    continue
                raise DeployError(f"foreign_container_holds_name:{name}")

    @contextmanager
    def _docker_auth(self) -> Iterator[dict[str, str]]:
        """A throwaway DOCKER_CONFIG so the GHCR token is never left in a docker login."""
        if self._registry_credentials is None:
            yield {}
            return
        self.settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="docker-auth-", dir=self.settings.state_dir))
        try:
            auth = base64.b64encode(":".join(self._registry_credentials).encode()).decode()
            hosts = {self.settings.backend_repository.split("/", 1)[0],
                     self.settings.frontend_repository.split("/", 1)[0]}
            _write_private(directory / "config.json",
                           json.dumps({"auths": {host: {"auth": auth} for host in hosts}}))
            yield {**_base_env(), "DOCKER_CONFIG": str(directory)}
        finally:
            shutil.rmtree(directory)

    def _pull(self, images: Sequence[str]) -> None:
        with self._docker_auth() as env:
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

    def _backup(self) -> None:
        script = self.settings.pgbackrest_dir / "backup.sh"
        # backup.sh writes its evidence under ${HOME}/bfx; runuser sets HOME to the
        # backup user's home, exactly as bfx-pgbackrest-backup.service (User=ubuntu).
        self._check(["runuser", "-u", self.settings.backup_user, "--", str(script),
                     "--type", "diff"], code="backup_sh_failed", timeout=7200.0)

    def _migrate(self, prepared: Prepared, attempt: Attempt) -> int | None:
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
            return self._finish(attempt, "failed", f"migration_failed:{code}; schema unchanged; "
                                "running release untouched")
        attempt.migrations_applied = True
        stopped = self._stop_bot()
        return self._finish(attempt, "failed", "migration_partial_or_unverified; schema changed; "
                            f"no automatic rollback; {stopped}")

    def _compose_env(self, target: Target, klass: str) -> dict[str, str]:
        return {**_base_env(), "BFX_BACKEND_IMAGE": target.backend_image,
                "BFX_BACKEND_DIGEST": target.backend_digest,
                "BFX_FRONTEND_IMAGE": target.frontend_image,
                "BFX_FRONTEND_DIGEST": target.frontend_digest,
                "BFX_SOURCE_REVISION": target.revision, "BFX_CHANGE_CLASS": klass}

    def _compose_up(self, release_dir: Path, target: Target, klass: str) -> None:
        for image, digest in ((target.backend_image, target.backend_digest),
                              (target.frontend_image, target.frontend_digest)):
            if DIGEST.fullmatch(digest) is None or not image.endswith("@" + digest):
                raise DeployError("image_reference_not_digest_pinned")
        self._check(["docker", "compose", "-p", PROJECT, "-f",
                     str(release_dir / Path(COMPOSE_PATH).name),
                     "up", "--detach", "--no-deps", *SERVICES],
                    code="compose_up_failed", timeout=600.0, env=self._compose_env(target, klass))

    def _verify(self, target: Target, klass: str) -> None:
        result = self._check(["docker", "inspect", "--type", "container", *CONTAINERS.values()],
                             code="docker_inspect_failed", timeout=60.0)
        try:
            records = {str(record["Name"]).lstrip("/"): record for record in json.loads(result.stdout)}
        except (ValueError, KeyError, TypeError):
            raise DeployError("docker_inspect_unparsable") from None
        problems = []
        for service, container in CONTAINERS.items():
            backend = service in BACKEND_COMMANDS
            record = records.get(container)
            if record is None:
                problems.append(f"{service}:missing")
                continue
            violations = container_violations(
                record, service=service,
                image=target.backend_image if backend else target.frontend_image,
                digest=target.backend_digest if backend else target.frontend_digest,
                revision=target.revision, klass=klass, network=self.settings.network,
            )
            problems += [f"{service}:{violation}" for violation in violations]
        if problems:
            raise DeployError("container_hardening_mismatch:" + ",".join(problems))

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
        if result.returncode != 0:
            log(f"docker stop bfx-bot failed: {result.stderr.strip()[-500:]}")
            return "BOT_STOP_FAILED"
        return "bot stopped"

    def _unhealthy(self, view: LedgerView, attempt: Attempt, exc: DeployError) -> int:
        reason = exc.code
        previous = view.last_success
        if attempt.migrations_applied or previous is None:
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
        log(f"rolling back to {previous.source_revision}")
        target = Target(
            revision=previous.source_revision, backend_digest=previous.backend_digest,
            frontend_digest=previous.frontend_digest,
            backend_image=f"{self.settings.backend_repository}@{previous.backend_digest}",
            frontend_image=f"{self.settings.frontend_repository}@{previous.frontend_digest}",
        )
        release_dir = self._materialize(previous.source_revision)
        self._pull((target.backend_image, target.frontend_image))
        self._compose_up(release_dir, target, previous.change_class)
        self._verify(target, previous.change_class)
        self._wait_healthy()

    def _after_success(self, view: LedgerView, target: Target) -> list[str]:
        warnings = []
        if self.settings.local_alias:
            # Transitional (until T12): the DR verifier (docker-compose.dr.yml) and the
            # weekly report still name bfx-bot:local; keep that alias on the running
            # backend so neither drifts behind production. Never used to deploy.
            tagged = self._run(["docker", "tag", target.backend_image, self.settings.local_alias],
                               timeout=60.0)
            if tagged.returncode != 0:
                warnings.append("local_alias_failed")
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

    def _finish(self, attempt: Attempt, outcome: str, detail: str) -> int:
        target = attempt.target
        text = _sanitize(f"class={attempt.klass}({attempt.class_detail}); {detail}")
        entry = LedgerEntry(
            started_at=attempt.started_at, finished_at=_now_iso(self._clock),
            source_revision=target.revision, backend_digest=target.backend_digest,
            frontend_digest=target.frontend_digest, change_class=attempt.klass,
            migrations_applied=attempt.migrations_applied, outcome=outcome, detail=text,
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
        message = (f"deploy {outcome}: {target.revision[:12]} class={attempt.klass} "
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
    parser.add_argument("--pgbackrest-dir", type=Path,
                        help="default: <mirror>/deploy/vm/pgbackrest")
    parser.add_argument("--backup-user", help="run backup.sh as this user (default: mirror owner)")
    parser.add_argument("--dry-run", action="store_true",
                        help="discover, fetch, classify, pull and compare schema; change nothing")
    parser.add_argument("--retry", action="store_true",
                        help="attempt a digest whose last attempt failed or rolled back")
    parser.add_argument("--force-material", action="store_true",
                        help="raise this release to material (a class can never be lowered)")
    parser.add_argument("--rollback-drill", action="store_true",
                        help="deploy the new release, then take the real rollback path on purpose "
                             "(refused when migrations are pending); run with the timer stopped, "
                             "then deploy it for real with --retry")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
        pgbackrest_dir=args.pgbackrest_dir or mirror / "deploy/vm/pgbackrest",
        backup_user=args.backup_user or owner, dry_run=args.dry_run, retry=args.retry,
        force_material=args.force_material, rollback_drill=args.rollback_drill,
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
    deployer = Deployer(
        settings, runner=subprocess_runner,
        registry=RegistryClient(credentials=credentials),
        ledger=PsqlLedger(subprocess_runner, container=settings.postgres_container,
                          db_user=settings.db_user, db_name=settings.db_name),
        notify=notify, registry_credentials=credentials,
    )
    return deployer.run()


if __name__ == "__main__":
    raise SystemExit(main())
