#!/usr/bin/env python3
"""Build the public proof site, upload it to Cloudflare Pages, then check it is served.

ADR 2026-08-31 D6: the public site is static files on Cloudflare Pages, so the
internet never reaches the money VM. bfx-public-site.service (its own weekly timer,
or by hand) runs this file twice, as two users:

`build` -- root (the unit's `ExecStartPre=+`): checks the config and the token file
  are root 0600 (bfx_deploy.protected_secret_file), creates BUILD_DIR (uid 1000,
  0755) inside the unit's RuntimeDirectory, and runs docker-compose.public-site.yml
  on the plan bfx_weekly_report.build_plan resolves for the weekly job: the deployed
  backend digest and the research role (scripts/build_public_site.py, no Cloudflare
  token). A failed build fails the unit. systemd removes the RuntimeDirectory when
  the unit stops, so whatever `publish` finds there is this run's build.

`publish` -- the unit's DynamicUser, never root; the stdlib and this release only:
  1. the build: exactly SITE_FILES (the list in modules/live_validation/public_site.py;
     tests keep the two equal), regular files only, no symlink anywhere (uid 1000 owns
     the directory), each under Pages' 25 MiB limit;
  2. the config: BFX_PAGES_ACCOUNT_ID and BFX_PAGES_PROJECT from the environment
     (EnvironmentFile=/opt/bfx/runtime/public-site.env), the token from
     $CREDENTIALS_DIRECTORY/cloudflare-api-token (LoadCredential= of
     /opt/bfx/runtime/secrets/public-site/CLOUDFLARE_API_TOKEN). The token is an
     account-scoped "Pages Write" token limited to the VM's egress IP: Cloudflare has
     no per-project Pages permission;
  3. Pages Direct Upload (the API wrangler uses): an upload JWT, the assets Pages
     does not already hold, then one production deployment whose manifest maps each
     path to its asset key; `_headers` travels as its own form field, as wrangler
     sends it. The key is the first 32 hex digits of sha256(base64 + extension);
     Pages only needs it to be a stable content key (measured 2026-10-23: a sha256
     key deploys and serves; wrangler uses blake3);
  4. the check: https://<project>.pages.dev/proof-summary.json must return the
     uploaded bytes within VERIFY_SECONDS, and the page must carry the site's CSP.

Each step exits 0 only on success (publish: after the check passes); anything else
exits 1 and the unit's OnFailure=bfx-alert@ sends the Telegram message. Errors name
a step and an HTTP status or Cloudflare error code; the token and the upload JWT are
never logged.

`publish --dry-run` checks the build and the config and prints the plan (paths,
sizes, keys); it sends nothing. `build` has no dry run: its directory exists only
while the unit runs (`bfx_weekly_report.py --dry-run` shows the same plan).
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx_weekly_report = _load("_bfx_ops_weekly_report", _HERE / "bfx_weekly_report.py")
bfx_deploy = bfx_weekly_report.bfx_deploy
DeployError = bfx_deploy.DeployError

SITE_FILES = (
    "index.html", "en/index.html", "proof-summary.json",
    "funding-rates-fUST.csv", "funding-rates-fUSD.csv", "_headers",
)
# RuntimeDirectory=bfx-public-site; the bind source in docker-compose.public-site.yml
# (compose_policy.PUBLIC_SITE_OUT), where the container writes site/.
BUILD_DIR = Path("/run/bfx-public-site/build")
SITE_DIR = BUILD_DIR / "site"
BUILD_UID = 1000  # the image's appuser
COMPOSE_FILE = "docker-compose.public-site.yml"
PROJECT = "bfx-public-site"
SERVICE = "public-site-build"
CONFIG_FILE = "public-site.env"
CONFIG_KEYS = ("BFX_PAGES_ACCOUNT_ID", "BFX_PAGES_PROJECT")
TOKEN_FILE = Path("secrets/public-site/CLOUDFLARE_API_TOKEN")
CREDENTIAL = "cloudflare-api-token"  # the unit's LoadCredential= name
API = "https://api.cloudflare.com/client/v4"
MAX_FILE_BYTES = 25 * 1024 * 1024
VERIFY_SECONDS = 120
CONTENT_TYPES = {"html": "text/html; charset=utf-8", "json": "application/json",
                 "csv": "text/csv; charset=utf-8"}
CSP_MARKER = "default-src 'none'"
# pages.dev answers urllib's default `Python-urllib/3.x` with 403 (measured 2026-10-24 from
# the VM: the same URL is 200 for curl and for this agent), so every request names itself.
USER_AGENT = "bfx-public-site/1"

# (method, url, headers, body) -> (status, body, response headers)
Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes, Mapping[str, str]]]


class PublishError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def log(message: str) -> None:
    print(f"bfx-public-site: {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class Asset:
    path: str
    body: bytes

    @property
    def extension(self) -> str:
        return self.path.rsplit(".", 1)[-1] if "." in self.path.rsplit("/", 1)[-1] else ""

    @property
    def b64(self) -> str:
        return base64.b64encode(self.body).decode()

    @property
    def key(self) -> str:
        return hashlib.sha256((self.b64 + self.extension).encode()).hexdigest()[:32]


_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


def _open_at(name: str, flags: int, *, dir_fd: int | None, relative: str) -> int:
    try:
        return os.open(name, flags | _NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        kind = "site_symlink" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "site_unreadable"
        raise PublishError(f"{kind}:{relative}") from None


def _read_tree(dir_fd: int, prefix: str, found: dict[str, bytes]) -> None:
    """Walk by descriptor, never by path: uid 1000 owns the build dir, so a file or a
    directory swapped for a symlink mid-walk must not lead the reader anywhere else.
    Every component is opened with O_NOFOLLOW relative to the directory already open."""
    for name in sorted(os.listdir(dir_fd)):
        relative = f"{prefix}{name}"
        fd = _open_at(name, os.O_RDONLY | os.O_NONBLOCK, dir_fd=dir_fd, relative=relative)
        try:
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                _read_tree(fd, f"{relative}/", found)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise PublishError(f"site_not_regular:{relative}")
            if info.st_size > MAX_FILE_BYTES:
                raise PublishError(f"site_file_too_large:{relative}")
            with os.fdopen(os.dup(fd), "rb") as stream:
                body = stream.read(MAX_FILE_BYTES + 1)
            if len(body) > MAX_FILE_BYTES:
                raise PublishError(f"site_file_too_large:{relative}")
            found[relative] = body
        finally:
            os.close(fd)


def collect(site_dir: Path) -> dict[str, bytes]:
    """The build, by published path; refuses anything but exactly SITE_FILES."""
    try:
        root = os.open(site_dir, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    except OSError:
        raise PublishError("site_dir_missing") from None
    found: dict[str, bytes] = {}
    try:
        _read_tree(root, "", found)
    finally:
        os.close(root)
    if set(found) != set(SITE_FILES):
        extra = sorted(set(found) - set(SITE_FILES))
        missing = sorted(set(SITE_FILES) - set(found))
        raise PublishError(f"site_files_mismatch:extra={','.join(extra)}:missing={','.join(missing)}")
    return {name: found[name] for name in SITE_FILES}


@dataclass(frozen=True, slots=True)
class Config:
    account_id: str
    project: str
    token: str = field(repr=False)  # never logged or printed

    @property
    def site_url(self) -> str:
        return f"https://{self.project}.pages.dev"


def check_secret_files(runtime_dir: Path, *, secret_check: Callable[[Path], None]) -> None:
    """Root, before the build: systemd reads both files whatever their mode, so the
    mode bfx_deploy holds every secret file to is checked here."""
    for path in (runtime_dir / CONFIG_FILE, runtime_dir / TOKEN_FILE):
        secret_check(path)


def load_config(environ: Mapping[str, str]) -> Config:
    """The unprivileged publish step's config: the unit's environment and credential."""
    missing = [key for key in CONFIG_KEYS if not environ.get(key, "").strip()]
    if missing:
        raise DeployError(f"env_key_missing:{CONFIG_FILE}:{','.join(missing)}")
    credentials = environ.get("CREDENTIALS_DIRECTORY", "")
    if not credentials:
        raise DeployError("credential_missing")
    try:
        token = (Path(credentials) / CREDENTIAL).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise DeployError("credential_unreadable") from None
    if not token:
        raise DeployError("token_empty")
    return Config(environ["BFX_PAGES_ACCOUNT_ID"].strip(), environ["BFX_PAGES_PROJECT"].strip(), token)


def prepare_build_dir(build_dir: Path, *, uid: int = BUILD_UID) -> None:
    """The container's only writable host path: empty, owned by its uid, readable by the
    publish step. The RuntimeDirectory is new for every run, so an existing one is refused."""
    try:
        build_dir.mkdir(mode=0o755)
        os.chown(build_dir, uid, uid)
        build_dir.chmod(0o755)
    except OSError as exc:
        raise DeployError(f"build_dir_unusable:{exc.__class__.__name__}") from None


def build_plan(*, ops_dir: Path, ledger: object, runtime_dir: Path, state_dir: Path,
               backend_repository: str) -> bfx_weekly_report.Plan:
    return bfx_weekly_report.build_plan(
        ops_dir=ops_dir, ledger=ledger, runtime_dir=runtime_dir, state_dir=state_dir,
        backend_repository=backend_repository, secret_check=bfx_deploy.protected_secret_file,
        reports_dir=BUILD_DIR, compose_name=COMPOSE_FILE, project=PROJECT, service=SERVICE)


def urllib_transport(method: str, url: str, headers: Mapping[str, str],
                     body: bytes | None) -> tuple[int, bytes, Mapping[str, str]]:
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})
    except (urllib.error.URLError, OSError):
        return 0, b"", {}


def _api(transport: Transport, step: str, method: str, url: str, bearer: str, *,
         payload: object = None, body: bytes | None = None, content_type: str | None = None) -> object:
    headers = {"Authorization": f"Bearer {bearer}"}
    if payload is not None:
        body, content_type = json.dumps(payload).encode(), "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    status, raw, _ = transport(method, url, headers, body)
    try:
        answer = json.loads(raw)
    except ValueError:
        answer = None
    if status != 200 or not isinstance(answer, dict) or answer.get("success") is not True:
        codes = []
        if isinstance(answer, dict):
            codes = [str(e.get("code")) for e in answer.get("errors") or [] if isinstance(e, dict)]
        raise PublishError(f"{step}_failed:http_{status}:{','.join(codes) or 'no_code'}")
    return answer.get("result")


def _multipart(fields: Mapping[str, tuple[str | None, bytes]]) -> tuple[bytes, str]:
    boundary = f"bfx-{uuid.uuid4().hex}"
    parts = []
    for name, (filename, value) in fields.items():
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        parts.append(f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + value + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def publish(files: Mapping[str, bytes], config: Config, *, transport: Transport) -> str:
    """Upload and deploy; returns the deployment id."""
    project = f"{API}/accounts/{config.account_id}/pages/projects/{config.project}"
    assets = [Asset(path, body) for path, body in files.items() if path != "_headers"]
    upload = _api(transport, "upload_token", "GET", f"{project}/upload-token", config.token)
    jwt = upload.get("jwt") if isinstance(upload, dict) else None
    if not isinstance(jwt, str) or not jwt:
        raise PublishError("upload_token_failed:no_jwt")
    keys = sorted({asset.key for asset in assets})
    missing = _api(transport, "check_missing", "POST", f"{API}/pages/assets/check-missing", jwt,
                   payload={"hashes": keys})
    if not isinstance(missing, list):
        raise PublishError("check_missing_failed:shape")
    for asset in assets:
        if asset.key in missing:
            _api(transport, "upload", "POST", f"{API}/pages/assets/upload", jwt, payload=[{
                "key": asset.key, "value": asset.b64, "base64": True,
                "metadata": {"contentType": CONTENT_TYPES[asset.extension]},
            }])
            missing.remove(asset.key)
    _api(transport, "upsert_hashes", "POST", f"{API}/pages/assets/upsert-hashes", jwt,
         payload={"hashes": keys})
    manifest = json.dumps({f"/{asset.path}": asset.key for asset in assets}).encode()
    body, content_type = _multipart({"manifest": (None, manifest),
                                     "_headers": ("_headers", files["_headers"])})
    deployment = _api(transport, "deploy", "POST", f"{project}/deployments", config.token,
                      body=body, content_type=content_type)
    deployment_id = deployment.get("id") if isinstance(deployment, dict) else None
    if not isinstance(deployment_id, str):
        raise PublishError("deploy_failed:no_id")
    return deployment_id


def verify(files: Mapping[str, bytes], config: Config, *, transport: Transport,
           sleep: Callable[[float], None] | None = None, clock: Callable[[], float] = time.monotonic,
           timeout: float | None = None) -> None:
    """Production serves the uploaded proof-summary.json, and the page carries the CSP."""
    sleep = sleep or time.sleep
    deadline = clock() + (VERIFY_SECONDS if timeout is None else timeout)
    want = hashlib.sha256(files["proof-summary.json"]).hexdigest()
    while True:
        status, body, _ = transport("GET", f"{config.site_url}/proof-summary.json?v={uuid.uuid4().hex}",
                                    {"Cache-Control": "no-cache"}, None)
        if status == 200 and hashlib.sha256(body).hexdigest() == want:
            break
        if clock() >= deadline:
            raise PublishError(f"verify_failed:proof_summary:http_{status}")
        sleep(5)
    status, _, headers = transport("GET", f"{config.site_url}/", {}, None)
    csp = next((v for k, v in headers.items() if k.lower() == "content-security-policy"), "")
    if status != 200 or CSP_MARKER not in csp:
        raise PublishError(f"verify_failed:index:http_{status}")


def run_build(args: argparse.Namespace, *,
              execute: Callable[[list[str], Mapping[str, str]], int] | None = None) -> int:
    if os.geteuid() != 0:
        log("build must run as root (reads /opt/bfx/runtime and the deployments ledger, drives docker)")
        return 2
    ledger = bfx_deploy.PsqlLedger(bfx_deploy.subprocess_runner, container=args.postgres_container,
                                   db_user=args.db_user, db_name=args.db_name)
    try:
        check_secret_files(args.runtime_dir, secret_check=bfx_deploy.protected_secret_file)
        prepare_build_dir(BUILD_DIR)
        plan = build_plan(ops_dir=_HERE, ledger=ledger, runtime_dir=args.runtime_dir,
                          state_dir=args.state_dir, backend_repository=args.backend_repository)
    except DeployError as exc:
        log(f"cannot build: {exc.code}")
        return 1
    log(f"building with {plan.backend_image}")
    if execute is None:
        def execute(argv: list[str], env: Mapping[str, str]) -> int:
            return subprocess.run(argv, env=dict(env), check=False).returncode
    returncode = execute(plan.argv, plan.env)
    if returncode != 0:
        log(f"build failed with exit code {returncode}")
        return 1
    return 0


def run_publish(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    if os.geteuid() == 0:
        log("publish never runs as root: start bfx-public-site.service, whose DynamicUser "
            "gets the token as a credential")
        return 2
    try:
        files = collect(args.site_dir)
        config = load_config(environ)
    except (PublishError, DeployError) as exc:
        log(f"cannot publish: {exc.code}")
        return 1
    if args.dry_run:
        print(json.dumps({"project": config.project, "site_url": config.site_url,
                          "files": {path: {"bytes": len(body), "key": Asset(path, body).key}
                                    for path, body in files.items()}}, indent=2))
        return 0
    try:
        deployment_id = publish(files, config, transport=urllib_transport)
        log(f"deployment {deployment_id} created; checking {config.site_url}")
        verify(files, config, transport=urllib_transport)
    except PublishError as exc:
        log(f"publish failed: {exc.code}")
        return 1
    log(f"published {len(files)} files to {config.site_url} (deployment {deployment_id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    defaults = bfx_deploy.Settings()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    steps = parser.add_subparsers(dest="step", required=True)
    build = steps.add_parser("build", help="root: run this run's one-shot build container")
    build.add_argument("--runtime-dir", type=Path, default=defaults.runtime_dir)
    build.add_argument("--state-dir", type=Path, default=defaults.state_dir)
    build.add_argument("--backend-repository", default=defaults.backend_repository)
    build.add_argument("--postgres-container", default=defaults.postgres_container)
    build.add_argument("--db-user", default=defaults.db_user)
    build.add_argument("--db-name", default=defaults.db_name)
    publish_step = steps.add_parser("publish", help="unprivileged: upload the build and check it")
    publish_step.add_argument("--site-dir", type=Path, default=SITE_DIR)
    publish_step.add_argument("--dry-run", action="store_true",
                              help="check the build and the config, print the plan; send nothing")
    args = parser.parse_args(argv)
    if args.step == "build":
        return run_build(args)
    return run_publish(args, os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
