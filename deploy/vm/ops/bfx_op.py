#!/usr/bin/env python3
"""bfx-op: run one operator CLI command as the tailnet identity of this SSH session.

    sudo bfx-op [--dry-run] <subcommand> [args...]     # inside a Tailscale SSH session

Installed by install.sh as /usr/local/sbin/bfx-op, running this file from the
current host tooling release. sudo keeps exactly one variable for it,
SSH_CONNECTION (/etc/sudoers.d/bfx-op); everything the operator compose file
interpolates is built here, never taken from the caller.

One call:
  1. the peer: the first field of SSH_CONNECTION must be a tailnet address
     (100.64.0.0/10, fd7a:115c:a1e0::/48), else exit 77 -- a public sshd login or
     the OCI serial console is not an operator session;
  2. the identity: `tailscale whois --json <peer>` -> UserProfile.ID (passed as
     BFX_OPERATOR_TAILNET_IDENTITY), LoginName and Node.Name (journal only). The
     CLI compares the ID with operator.env's BFX_OPERATOR_TAILNET_USER_ID; this
     file never compares or rewrites it, and no flag names an actor;
  3. the image: the ledger's newest `deployed` row and its revision's live.env,
     as bfx-weekly-report resolves them; operator.env must be root 0600 (Compose
     reads it, this file does not);
  4. `docker compose -p bfx-operator -f <this release>/ops/docker-compose.operator.yml
     run --rm -T operator <subcommand> ...`: no TTY, so the CLI's stderr line naming
     the request stays apart from its JSON on stdout. stdin is closed, except for
     rotate-credential, whose key and secret are read here (without echo on a
     terminal, else two lines) and handed to the CLI as two lines of its stdin;
  5. the journal (syslog ident bfx-op): who, from where, which subcommand, the
     request id and the exit code. Never other arguments, never stdin.

Trust boundary: any root-equivalent shell can set SSH_CONNECTION and pass as
another peer (ubuntu's sudo is NOPASSWD). Who may open a session is Tailscale
SSH's check mode; this binds an honest session's identity to its requests and
records it. Exit codes are the CLI's, plus 77 (no tailnet identity), 1 (cannot
resolve what to run) and 2 (not root, or an option before the subcommand).
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import ipaddress
import json
import os
import re
import subprocess
import sys
import syslog
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


weekly = _load("_bfx_ops_weekly_report", _HERE / "bfx_weekly_report.py")
bfx_deploy = weekly.bfx_deploy  # one copy of bfx_deploy, so DeployError is one class
DeployError = bfx_deploy.DeployError

PROJECT = "bfx-operator"
SERVICE = "operator"
COMPOSE_FILE = "docker-compose.operator.yml"
OPERATOR_ENV = "operator.env"
LIVE_KEYS = ("BFX_DEPLOYMENT_ENV",)
TAILNET_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),
                    ipaddress.ip_network("fd7a:115c:a1e0::/48"))
WHOIS_TIMEOUT = 10.0
ROTATE = "rotate-credential"
EXIT_CANNOT_RUN = 1
EXIT_USAGE = 2
EXIT_IDENTITY = 77
REQUEST_LINE = re.compile(r"^operator_request request_id=(\S+) action=(\S+)")
# The only caller variables the child gets; everything else is built here.
CALLER_KEYS = ("PATH", "HOME")

Journal = Callable[[str], None]


def log(message: str) -> None:
    print(f"bfx-op: {message}", file=sys.stderr, flush=True)


def syslog_journal(line: str) -> None:
    syslog.openlog("bfx-op", 0, syslog.LOG_AUTH)
    syslog.syslog(syslog.LOG_NOTICE, line)


class RefusedError(Exception):
    """A controlled refusal before anything runs."""

    def __init__(self, code: str, exit_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


# --------------------------------------------------------------------------- identity


def tailnet_peer(env: Mapping[str, str]) -> str:
    """The session's peer address, when it is a tailnet address."""
    fields = env.get("SSH_CONNECTION", "").split()
    try:
        address = ipaddress.ip_address(fields[0]) if fields else None
    except ValueError:
        address = None
    if address is None or not any(address in network for network in TAILNET_NETWORKS):
        raise RefusedError("not_tailnet_peer", EXIT_IDENTITY)
    return str(address)


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: str
    login: str
    node: str
    peer: str


def whois(peer: str, *, runner: Any) -> Identity:
    """Who tailscaled says the peer is; refused unless it names a user ID."""
    try:
        result = runner(["tailscale", "whois", "--json", peer], timeout=WHOIS_TIMEOUT)
        document = json.loads(result.stdout) if result.returncode == 0 else None
    except (DeployError, ValueError):
        document = None
    if not isinstance(document, dict) or not isinstance(document.get("UserProfile"), dict):
        raise RefusedError("tailnet_whois_failed", EXIT_IDENTITY)
    profile = document["UserProfile"]
    user_id = profile.get("ID")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise RefusedError("tailnet_whois_failed", EXIT_IDENTITY)
    node = document.get("Node")
    return Identity(user_id=str(user_id), login=str(profile.get("LoginName") or "-"),
                    node=str(node.get("Name") or "-") if isinstance(node, dict) else "-",
                    peer=peer)


# --------------------------------------------------------------------------- arguments


@dataclass(frozen=True, slots=True)
class Call:
    dry_run: bool
    command: str
    args: tuple[str, ...]  # everything after the wrapper's own options, for the CLI


def parse_args(argv: Sequence[str]) -> Call:
    """Only --dry-run may precede the subcommand; the rest goes to the CLI untouched."""
    rest = list(argv)
    dry_run = bool(rest) and rest[0] == "--dry-run"
    if dry_run:
        rest = rest[1:]
    if not rest or rest[0].startswith("-"):
        raise RefusedError("usage", EXIT_USAGE)
    return Call(dry_run=dry_run, command=rest[0], args=tuple(rest))


@dataclass(frozen=True, slots=True)
class NewCredential:
    """The key and secret for rotate-credential; never rendered."""

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def as_stdin(self) -> str:
        return f"{self.api_key}\n{self.api_secret}\n"


def read_new_credential(stdin: TextIO, *, prompt: Callable[[str], str] = getpass.getpass,
                        ) -> NewCredential:
    """Without echo on a terminal, else two lines; the CLI validates them."""
    if stdin.isatty():
        api_key, api_secret = prompt("Bitfinex API key: "), prompt("Bitfinex API secret: ")
    else:
        api_key, api_secret = stdin.readline(), stdin.readline()
    return NewCredential(api_key.rstrip("\r\n"), api_secret.rstrip("\r\n"))


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True, slots=True)
class Plan:
    compose_file: Path
    tooling_revision: str
    deployed_revision: str
    backend_image: str
    env: Mapping[str, str]  # no secrets: operator.env is read by Compose, not here

    def argv(self, call: Call) -> list[str]:
        return ["docker", "compose", "-p", PROJECT, "-f", str(self.compose_file),
                "run", "--rm", "-T", SERVICE, *call.args]

    def describe(self, call: Call, identity: Identity) -> dict[str, object]:
        return {"compose_file": str(self.compose_file), "tooling_revision": self.tooling_revision,
                "deployed_revision": self.deployed_revision, "backend_image": self.backend_image,
                "tailnet_user_id": identity.user_id, "login": identity.login,
                "node": identity.node, "peer": identity.peer, "argv": self.argv(call)}


def _live_values(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise DeployError(f"env_file_unreadable:{path.name}") from None
    values = bfx_deploy.parse_env_file(text, name=path.name, compose=False)
    missing = [key for key in LIVE_KEYS if not values.get(key, "").strip()]
    if missing:
        raise DeployError(f"env_key_missing:{path.name}:{','.join(missing)}")
    return {key: values[key] for key in LIVE_KEYS}


def build_plan(
    *, ops_dir: Path, ledger: Any, runtime_dir: Path, state_dir: Path,
    backend_repository: str, secret_check: Callable[[Path], None], identity: Identity,
    caller_env: Mapping[str, str],
) -> Plan:
    compose_file = ops_dir / COMPOSE_FILE
    if not compose_file.is_file():
        raise DeployError("operator_compose_missing")
    secret_check(runtime_dir / OPERATOR_ENV)
    revision = weekly.tooling_revision(ops_dir)
    view = ledger.read()
    deployed = view.last_success if view.exists else None
    if deployed is None:
        raise DeployError("no_deployed_release")
    digest = deployed.backend_digest
    if bfx_deploy.DIGEST.fullmatch(digest) is None \
            or bfx_deploy.REVISION.fullmatch(deployed.source_revision) is None:
        raise DeployError("ledger_row_invalid")
    image = f"{backend_repository}@{digest}"
    live_env = state_dir / "releases" / deployed.source_revision / "live.env"
    env = {
        **{key: caller_env[key] for key in CALLER_KEYS if caller_env.get(key)},
        **_live_values(live_env),
        "BFX_BACKEND_IMAGE": image,
        "BFX_BACKEND_DIGEST": digest,
        "BFX_SOURCE_REVISION": deployed.source_revision,
        "BFX_OPERATOR_TAILNET_IDENTITY": identity.user_id,
    }
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return Plan(compose_file=compose_file, tooling_revision=revision,
                deployed_revision=deployed.source_revision, backend_image=image, env=env)


# --------------------------------------------------------------------------- run


Execute = Callable[[list[str], Mapping[str, str], str | None, Callable[[str], None]], int]


def subprocess_execute(argv: list[str], env: Mapping[str, str], stdin_text: str | None,
                       on_stderr: Callable[[str], None]) -> int:
    """Run with stdout inherited, stderr streamed line by line, stdin closed or given."""
    with subprocess.Popen(argv, env=dict(env), text=True, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
                          ) as process:
        if stdin_text is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin_text)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
        assert process.stderr is not None
        for line in process.stderr:
            on_stderr(line)
        return process.wait()


def run(plan: Plan, call: Call, identity: Identity, *, journal: Journal,
        execute: Execute = subprocess_execute, credential: NewCredential | None = None,
        stderr: TextIO | None = None) -> int:
    out = stderr if stderr is not None else sys.stderr
    who = (f"login={identity.login} tailnet_user_id={identity.user_id} node={identity.node} "
           f"peer={identity.peer} command={call.command}")
    start = f"start {who} revision={plan.deployed_revision[:12]}"
    if plan.tooling_revision != plan.deployed_revision:
        log(f"warning: compose file from tooling release {plan.tooling_revision[:12]}, image "
            f"from deployed release {plan.deployed_revision[:12]}; running anyway")
        start += f" tooling_mismatch={plan.tooling_revision[:12]}"
    journal(start)
    seen: dict[str, str] = {}

    def on_stderr(line: str) -> None:
        out.write(line)
        out.flush()
        match = REQUEST_LINE.match(line)
        if match is not None:
            seen["request_id"], seen["action"] = match.group(1), match.group(2)

    returncode = execute(plan.argv(call), plan.env,
                         credential.as_stdin() if credential is not None else None, on_stderr)
    journal(f"end {who} request_id={seen.get('request_id', '-')} "
            f"action={seen.get('action', '-')} exit={returncode}")
    return returncode


def _parser() -> argparse.ArgumentParser:
    defaults = bfx_deploy.Settings()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-dir", type=Path, default=defaults.runtime_dir)
    parser.add_argument("--state-dir", type=Path, default=defaults.state_dir)
    parser.add_argument("--backend-repository", default=defaults.backend_repository)
    parser.add_argument("--postgres-container", default=defaults.postgres_container)
    parser.add_argument("--db-user", default=defaults.db_user)
    parser.add_argument("--db-name", default=defaults.db_name)
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         journal: Journal = syslog_journal, runner: Any = None, ledger: Any = None,
         execute: Execute = subprocess_execute, stdin: TextIO | None = None,
         secret_check: Callable[[Path], None] | None = None,
         is_root: Callable[[], bool] = lambda: os.geteuid() == 0) -> int:
    environ = os.environ if env is None else env
    arguments = list(sys.argv[1:] if argv is None else argv)
    settings = _parser().parse_args([])
    peer = "-"
    try:
        if not is_root():
            raise RefusedError("not_root", EXIT_USAGE)
        call = parse_args(arguments)
        peer = tailnet_peer(environ)
        identity = whois(peer, runner=runner or bfx_deploy.subprocess_runner)
        plan = build_plan(
            ops_dir=_HERE,
            ledger=ledger or bfx_deploy.PsqlLedger(
                bfx_deploy.subprocess_runner, container=settings.postgres_container,
                db_user=settings.db_user, db_name=settings.db_name),
            runtime_dir=settings.runtime_dir, state_dir=settings.state_dir,
            backend_repository=settings.backend_repository,
            secret_check=secret_check or bfx_deploy.protected_secret_file,
            identity=identity, caller_env=environ)
    except RefusedError as exc:
        log(f"refused: {exc.code}")
        if exc.code != "not_root":
            journal(f"refused reason={exc.code} peer={peer}")
        return exc.exit_code
    except DeployError as exc:
        log(f"cannot run: {exc.code}")
        journal(f"refused reason={exc.code} peer={peer}")
        return EXIT_CANNOT_RUN
    if call.dry_run:
        print(json.dumps(plan.describe(call, identity), indent=2))
        return 0
    credential = (read_new_credential(stdin if stdin is not None else sys.stdin)
                  if call.command == ROTATE else None)
    return run(plan, call, identity, journal=journal, execute=execute, credential=credential)


if __name__ == "__main__":
    raise SystemExit(main())
