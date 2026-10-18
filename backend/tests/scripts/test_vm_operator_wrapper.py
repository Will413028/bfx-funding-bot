"""bfx-op, the host wrapper that runs the operator CLI as the session's tailnet identity.

deploy/vm/ops/bfx_op.py resolves the peer from SSH_CONNECTION, asks tailscaled who it
is, and runs one `docker compose run --rm -T operator ...` of the deployed backend; the
CLI compares the identity (tests/apps/test_operator_cli.py). These tests pin what the
wrapper hands the CLI and what it records, with fake whois, ledger and execution.
"""
from __future__ import annotations

import http.server
import importlib.util
import io
import json
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = "ghcr.io/will413028/bfx-funding-bot-backend"
DIGEST = "sha256:" + "3" * 64
REV = "b" * 40
OLD_REV = "a" * 40
TAILNET_ID = 4182486213003443
PEER = "100.81.235.46"
KEY, SECRET = "k3yAAAAbbbbCCCC", "s3cretDDDDeeeeFFFF"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


op = _load("vm_ops_bfx_op_under_test", ROOT / "deploy/vm/ops/bfx_op.py")
bfx = op.bfx_deploy


class FakeLedger:
    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self.rows = rows  # (revision, backend digest, outcome), oldest first

    def read(self) -> Any:
        successes = [i for i, row in enumerate(self.rows) if row[2] == "deployed"]

        def row(index: int) -> Any:
            revision, digest, outcome = self.rows[index]
            return bfx.LedgerRow(id=index + 1, attempt_id=f"a-{index}", source_revision=revision,
                                 backend_digest=digest, frontend_digest="sha256:" + "f" * 64,
                                 outcome=outcome, migrations_applied=False)

        return bfx.LedgerView(exists=True, last_attempt=row(len(self.rows) - 1) if self.rows else None,
                              last_success=row(successes[-1]) if successes else None)


def whois_document(user_id: object = TAILNET_ID) -> str:
    return json.dumps({"Node": {"Name": "wills-macbook-pro.tail357fef.ts.net."},
                       "UserProfile": {"ID": user_id, "LoginName": "Will413028@github"}})


class FakeRunner:
    """tailscale whois only."""

    def __init__(self, stdout: str = "", returncode: int = 0, error: Exception | None = None) -> None:
        self.stdout, self.returncode, self.error = stdout or whois_document(), returncode, error
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, input_text=None, env=None):
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        return bfx.CommandResult(self.returncode, self.stdout, "")


class FakeExecute:
    def __init__(self, stderr_lines: tuple[str, ...] = (), returncode: int = 0) -> None:
        self.stderr_lines, self.returncode = stderr_lines, returncode
        self.calls: list[tuple[list[str], dict[str, str], str | None]] = []

    def __call__(self, argv, env, stdin_text, on_stderr) -> int:
        self.calls.append((list(argv), dict(env), stdin_text))
        for line in self.stderr_lines:
            on_stderr(line)
        return self.returncode


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def host(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    release = tmp_path / "bfx-ops/releases" / REV
    (release / "ops").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/vm/ops/docker-compose.operator.yml", release / "ops" / op.COMPOSE_FILE)
    (release / ".installed").write_text(REV + "\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "operator.env").write_text("DATABASE_URL=postgresql://bfx_operator:pw@x/bfx\n")
    state = tmp_path / "state"
    (state / "releases" / REV).mkdir(parents=True)
    shutil.copy(ROOT / "deploy/vm/live.env", state / "releases" / REV / "live.env")
    monkeypatch.setattr(op, "_HERE", release / "ops")
    monkeypatch.setattr(op, "_parser", lambda: _parser_for(runtime, state))
    return {"ops": release / "ops", "runtime": runtime, "state": state}


def _parser_for(runtime: Path, state: Path) -> Any:
    parser = op.argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-dir", type=Path, default=runtime)
    parser.add_argument("--state-dir", type=Path, default=state)
    parser.add_argument("--backend-repository", default=BACKEND)
    parser.add_argument("--postgres-container", default="bfx-postgres")
    parser.add_argument("--db-user", default="owner")
    parser.add_argument("--db-name", default="bfx")
    return parser


def _env(**changes: str | None) -> dict[str, str]:
    env = {"SSH_CONNECTION": f"{PEER} 51515 100.93.84.51 22", "PATH": "/usr/bin:/bin",
           "HOME": "/root", "SUDO_USER": "ubuntu"}
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _call(argv: list[str], *, env: Mapping[str, str] | None = None,
          runner: FakeRunner | None = None, execute: FakeExecute | None = None,
          ledger: FakeLedger | None = None, stdin: io.StringIO | None = None,
          secret_check: Callable[[Path], None] | None = None,
          ) -> tuple[int, list[str], FakeExecute, FakeRunner]:
    journal: list[str] = []
    execute = execute or FakeExecute()
    runner = runner or FakeRunner()
    code = op.main(argv, env=_env() if env is None else env, journal=journal.append,
                   runner=runner, ledger=ledger or FakeLedger([(REV, DIGEST, "deployed")]),
                   execute=execute, stdin=stdin or io.StringIO(""),
                   secret_check=secret_check or (lambda _path: None), is_root=lambda: True)
    return code, journal, execute, runner


# --------------------------------------------------------------------------- identity


@pytest.mark.parametrize("stdout", [
    whois_document(None), whois_document("4182486213003443"), whois_document(0),
    whois_document(-5), whois_document(True), json.dumps({"Node": {}}), "not json", "[]",
])
def test_whois_without_a_user_id_refuses_before_compose(host, stdout) -> None:
    code, journal, execute, _ = _call(["status"], runner=FakeRunner(stdout))
    assert code == 77 and execute.calls == []
    assert journal == [f"refused reason=tailnet_whois_failed peer={PEER}"]


@pytest.mark.parametrize("runner", [
    FakeRunner(returncode=1),
    FakeRunner(error=bfx.CommandError("timeout:tailscale whois", timed_out=True)),
    FakeRunner(error=bfx.CommandError("not_executable:tailscale whois:FileNotFoundError")),
])
def test_whois_failing_refuses(host, runner) -> None:
    code, _, execute, _ = _call(["status"], runner=runner)
    assert code == 77 and execute.calls == []


@pytest.mark.parametrize("connection", [
    None, "", "   ", "203.0.113.5 1 10.0.0.2 22", "not-an-ip 1 2 22", "100.128.0.1 1 2 22",
    "2001:db8::1 1 2 22",
])
def test_only_a_tailnet_peer_is_an_operator_session(host, connection) -> None:
    code, journal, execute, runner = _call(["status"], env=_env(SSH_CONNECTION=connection))
    assert code == 77 and execute.calls == [] and runner.calls == []
    assert journal == ["refused reason=not_tailnet_peer peer=-"]


@pytest.mark.parametrize("peer", ["100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::9f01:eb2e"])
def test_tailnet_addresses_of_both_families_pass(host, peer) -> None:
    code, _, execute, runner = _call(["status"], env=_env(SSH_CONNECTION=f"{peer} 1 2 22"))
    assert code == 0 and runner.calls == [["tailscale", "whois", "--json", peer]]
    assert len(execute.calls) == 1


def test_the_identity_is_whois_never_the_callers(host) -> None:
    # The caller's own claim, and a whois ID that is not the configured one, both
    # reach the CLI as exactly what whois said: the CLI compares, the wrapper never does.
    env = _env(BFX_OPERATOR_TAILNET_IDENTITY="1", BFX_OPERATOR_TAILNET_USER_ID="1")
    code, _, execute, _ = _call(["status"], env=env, runner=FakeRunner(whois_document(777)))
    assert code == 0
    (_, child_env, _), = execute.calls
    assert child_env["BFX_OPERATOR_TAILNET_IDENTITY"] == "777"
    assert "BFX_OPERATOR_TAILNET_USER_ID" not in child_env


def test_the_child_gets_only_path_home_and_what_the_wrapper_builds(host) -> None:
    env = _env(DOCKER_HOST="tcp://evil:2375", BFX_BACKEND_IMAGE="evil@sha256:" + "0" * 64,
               LD_PRELOAD="/tmp/x.so", COMPOSE_FILE="/tmp/evil.yml")
    _, _, execute, _ = _call(["status"], env=env)
    (_, child_env, _), = execute.calls
    assert set(child_env) == {"PATH", "HOME", "BFX_DEPLOYMENT_ENV", "BFX_BACKEND_IMAGE",
                              "BFX_BACKEND_DIGEST", "BFX_SOURCE_REVISION",
                              "BFX_OPERATOR_TAILNET_IDENTITY"}
    assert child_env["BFX_BACKEND_IMAGE"] == f"{BACKEND}@{DIGEST}"
    assert child_env["BFX_DEPLOYMENT_ENV"] == "prod" and child_env["PATH"] == "/usr/bin:/bin"


def test_the_child_env_fills_every_variable_the_compose_file_requires(host) -> None:
    text = (ROOT / "deploy/vm/ops/docker-compose.operator.yml").read_text(encoding="utf-8")
    required = set(op.re.findall(r"\$\{([A-Z_]+):\?", text))
    _, _, execute, _ = _call(["status"])
    (_, child_env, _), = execute.calls
    assert required <= set(child_env)


# --------------------------------------------------------------------------- arguments


@pytest.mark.parametrize("argv", [
    ["--actor", "someone", "status"], ["--foo", "status"], ["--dry-run", "--actor", "x", "status"],
    [], ["--dry-run"], ["--help"],
])
def test_no_option_but_dry_run_precedes_the_subcommand(host, argv) -> None:
    code, journal, execute, runner = _call(argv)
    assert code == 2 and execute.calls == [] and runner.calls == []
    assert journal == ["refused reason=usage peer=-"]


def test_the_compose_command_is_fixed_and_the_rest_goes_to_the_cli(host) -> None:
    argv = ["kill", "--reason", "drill --actor x"]
    _, _, execute, _ = _call(argv)
    (child_argv, _, stdin_text), = execute.calls
    assert child_argv == ["docker", "compose", "-p", "bfx-operator", "-f",
                          str(host["ops"] / "docker-compose.operator.yml"),
                          "run", "--rm", "-T", "operator", *argv]
    assert stdin_text is None  # closed: nothing but rotate-credential reads stdin


def test_dry_run_prints_the_plan_and_runs_nothing(host, capsys) -> None:
    code, journal, execute, _ = _call(["--dry-run", "status"])
    assert code == 0 and execute.calls == [] and journal == []
    described = json.loads(capsys.readouterr().out)
    assert described["tailnet_user_id"] == str(TAILNET_ID) and described["peer"] == PEER
    assert described["argv"][-2:] == ["operator", "status"]


def test_not_root_refuses(host) -> None:
    journal: list[str] = []
    assert op.main(["status"], env=_env(), journal=journal.append, is_root=lambda: False) == 2
    assert journal == ["refused reason=not_root peer=-"]


# --------------------------------------------------------------------------- rotate-credential


def _rotate(stdin: io.StringIO) -> tuple[int, list[str], FakeExecute, str]:
    err = io.StringIO()
    journal: list[str] = []
    execute = FakeExecute(stderr_lines=(
        f"operator_request request_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee "
        f"action=rotate tailnet_identity={TAILNET_ID}\n",))
    plan = op.build_plan(
        ops_dir=op._HERE, ledger=FakeLedger([(REV, DIGEST, "deployed")]),
        runtime_dir=Path("/unused"), state_dir=op._parser().parse_args([]).state_dir,
        backend_repository=BACKEND, secret_check=lambda _p: None,
        identity=op.Identity(str(TAILNET_ID), "Will413028@github", "mac", PEER), caller_env=_env())
    call = op.parse_args(["rotate-credential", "--reason", "drill"])
    credential = op.read_new_credential(stdin, prompt=lambda _text: KEY if "key" in _text else SECRET)
    code = op.run(plan, call, op.Identity(str(TAILNET_ID), "Will413028@github", "mac", PEER),
                  journal=journal.append, execute=execute, credential=credential, stderr=err)
    rendered = repr(plan) + repr(credential) + json.dumps(plan.describe(call, op.Identity(
        str(TAILNET_ID), "l", "n", PEER)))
    return code, journal, execute, err.getvalue() + rendered


@pytest.mark.parametrize("stdin", [io.StringIO(f"{KEY}\n{SECRET}\n"), Terminal("")])
def test_the_new_key_reaches_the_cli_stdin_and_nothing_else(host, stdin) -> None:
    code, journal, execute, visible = _rotate(stdin)
    assert code == 0
    (argv, env, stdin_text), = execute.calls
    assert stdin_text == f"{KEY}\n{SECRET}\n"
    everything = " ".join(journal) + visible + " ".join(argv) + json.dumps(env)
    assert KEY not in everything and SECRET not in everything


def test_rotate_credential_through_main_reads_stdin_and_others_do_not(host) -> None:
    _, _, execute, _ = _call(["rotate-credential", "--reason", "r"],
                             stdin=io.StringIO(f"{KEY}\n{SECRET}\n"))
    assert execute.calls[0][2] == f"{KEY}\n{SECRET}\n"
    piped = io.StringIO(f"{KEY}\n{SECRET}\n")
    _, _, execute, _ = _call(["status"], stdin=piped)
    assert execute.calls[0][2] is None and piped.tell() == 0


# --------------------------------------------------------------------------- journal


def test_the_journal_names_who_what_and_the_request_never_the_arguments(host, capsys) -> None:
    line = (f"operator_request request_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee action=kill "
            f"tailnet_identity={TAILNET_ID}\n")
    code, journal, _, _ = _call(["kill", "--reason", "secret-ish reason text"],
                                execute=FakeExecute(stderr_lines=(line,), returncode=3))
    assert code == 3
    who = (f"login=Will413028@github tailnet_user_id={TAILNET_ID} "
           f"node=wills-macbook-pro.tail357fef.ts.net. peer={PEER} command=kill")
    assert journal == [f"start {who} revision={REV[:12]}",
                       f"end {who} request_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee "
                       "action=kill exit=3"]
    assert "reason text" not in " ".join(journal)
    assert line in capsys.readouterr().err  # the CLI's line still reaches the operator


def test_a_read_has_no_request_id(host) -> None:
    _, journal, _, _ = _call(["status"])
    assert journal[-1].endswith("request_id=- action=- exit=0")


def test_a_tooling_release_behind_the_deploy_runs_and_says_so(host, capsys) -> None:
    (host["ops"].parent / ".installed").write_text(OLD_REV + "\n")
    code, journal, execute, _ = _call(["status"])
    assert code == 0 and len(execute.calls) == 1
    assert journal[0].endswith(f"tooling_mismatch={OLD_REV[:12]}")
    assert "warning: compose file from tooling release" in capsys.readouterr().err


# --------------------------------------------------------------------------- what to run


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda h: None, "no_deployed_release"),
    (lambda h: (h["state"] / "releases" / REV / "live.env").unlink(), "env_file_unreadable:live.env"),
    (lambda h: (h["ops"] / "docker-compose.operator.yml").unlink(), "operator_compose_missing"),
    (lambda h: (h["ops"].parent / ".installed").unlink(), "tooling_release_unknown"),
])
def test_cannot_run_refuses_instead_of_guessing(host, mutate, code) -> None:
    mutate(host)
    rows = [(REV, DIGEST, "failed")] if code == "no_deployed_release" else [(REV, DIGEST, "deployed")]
    result, journal, execute, _ = _call(["status"], ledger=FakeLedger(rows))
    assert (result, execute.calls) == (1, [])
    assert journal == [f"refused reason={code} peer={PEER}"]


def test_an_unprotected_operator_env_stops_the_run(host) -> None:
    checked: list[Path] = []

    def refuse(path: Path) -> None:
        checked.append(path)
        raise bfx.DeployError(f"secret_file_mode_not_0600:{path.name}")

    code, journal, execute, _ = _call(["status"], secret_check=refuse)
    assert (code, execute.calls, checked) == (1, [], [host["runtime"] / "operator.env"])
    assert journal == [f"refused reason=secret_file_mode_not_0600:operator.env peer={PEER}"]


def test_the_real_secret_check_is_the_deployers() -> None:
    assert op.bfx_deploy.protected_secret_file is bfx.protected_secret_file
    assert op.DeployError is bfx.DeployError and op.weekly.bfx_deploy is bfx


def test_defaults_are_the_deployers() -> None:
    args = op._parser().parse_args([])
    defaults = bfx.Settings()
    assert (args.runtime_dir, args.state_dir, args.backend_repository, args.postgres_container) == (
        defaults.runtime_dir, defaults.state_dir, defaults.backend_repository,
        defaults.postgres_container)


@pytest.mark.parametrize(("stdin_text", "expected"), [
    (None, op.subprocess.DEVNULL), (f"{KEY}\n{SECRET}\n", op.subprocess.PIPE)])
def test_stdin_is_closed_unless_a_key_is_handed_over(monkeypatch, stdin_text, expected) -> None:
    # An inherited terminal would echo whatever is typed into a container without a TTY.
    seen: dict[str, Any] = {}

    class Process:
        def __init__(self, argv, **kwargs) -> None:
            seen.update(kwargs)
            self.stdin = io.StringIO() if kwargs["stdin"] == op.subprocess.PIPE else None
            self.stderr = iter(())

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(op.subprocess, "Popen", Process)
    assert op.subprocess_execute(["x"], {}, stdin_text, lambda _line: None) == 0
    assert seen["stdin"] == expected and seen["stderr"] == op.subprocess.PIPE
    assert "stdout" not in seen  # inherited: the JSON goes straight to the operator


def test_the_child_streams_stderr_and_keeps_stdout(tmp_path: Path) -> None:
    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        "a, b = sys.stdin.readline(), sys.stdin.readline()\n"
        "print('out', len(a.strip()), len(b.strip()))\n"
        "print('operator_request request_id=r1 action=rotate tailnet_identity=1', file=sys.stderr)\n")
    lines: list[str] = []
    code = op.subprocess_execute([sys.executable, str(script)], {"PATH": "/usr/bin:/bin"},
                                 f"{KEY}\n{SECRET}\n", lines.append)
    assert code == 0 and lines == ["operator_request request_id=r1 action=rotate tailnet_identity=1\n"]


# --------------------------------------------------------------------------- halt
#
# Mutations (one at a time; revert after each): halt requires the tailnet identity
# (test_halt_runs_from_the_console_as_host_root fails); every subcommand skips it
# (test_every_other_subcommand_still_needs_a_tailnet_peer fails); HALT_SCRIPT treats
# 502 as 0 (test_the_halt_script_exit_protocol[502-3] fails).

HALT_REASON = "venue looks wrong, stop now"


class NoLedger:
    """halt reads no ledger, operator.env or DB: it only reaches the running bot."""

    def read(self) -> Any:
        raise AssertionError("halt read the deploy ledger")


def _halt(argv: list[str], *, env: Mapping[str, str] | None = None,
          runner: FakeRunner | None = None, execute: FakeExecute | None = None,
          ) -> tuple[int, list[str], FakeExecute, FakeRunner]:
    def no_secret(path: Path) -> None:
        raise AssertionError(f"halt checked {path}")

    return _call(argv, env=env, runner=runner, execute=execute, ledger=NoLedger(),
                 secret_check=no_secret)


def _halt_argv(reason: str, actor: str) -> list[str]:
    return ["docker", "exec", "bfx-bot", "/app/.venv/bin/python", "-c", op.HALT_SCRIPT,
            reason, actor]


def test_halt_with_a_tailnet_identity_acts_as_the_login() -> None:
    code, journal, execute, runner = _halt(["halt", "--reason", HALT_REASON])
    assert code == 0 and runner.calls == [["tailscale", "whois", "--json", PEER]]
    (argv, env, stdin_text), = execute.calls
    assert argv == _halt_argv(HALT_REASON, "Will413028@github")
    assert stdin_text is None and set(env) == {"PATH", "HOME"}
    who = (f"command=halt identity=tailnet login=Will413028@github tailnet_user_id={TAILNET_ID} "
           f"node=wills-macbook-pro.tail357fef.ts.net. peer={PEER} actor=Will413028@github")
    assert journal == [f"start {who}", f"end {who} exit=0"]


@pytest.mark.parametrize("connection", [None, "", "203.0.113.5 1 10.0.0.2 22"])
def test_halt_runs_from_the_console_as_host_root(connection) -> None:
    # The OCI console (no SSH_CONNECTION) is exactly when the static halt is needed.
    code, journal, execute, runner = _halt(["halt", "--reason", HALT_REASON],
                                           env=_env(SSH_CONNECTION=connection))
    assert code == 0 and runner.calls == []
    (argv, _, _), = execute.calls
    assert argv == _halt_argv(HALT_REASON, "host-root")
    who = "command=halt identity=none login=- peer=- actor=host-root"
    assert journal == [f"start {who} no_identity=not_tailnet_peer", f"end {who} exit=0"]


@pytest.mark.parametrize("runner", [
    FakeRunner(returncode=1), FakeRunner(whois_document(None)),
    FakeRunner(error=bfx.CommandError("timeout:tailscale whois", timed_out=True)),
])
def test_halt_runs_when_whois_fails(runner) -> None:
    code, journal, execute, _ = _halt(["halt", "--reason", HALT_REASON], runner=runner)
    assert code == 0
    (argv, _, _), = execute.calls
    assert argv[-1] == "host-root"
    assert journal[0] == ("start command=halt identity=none login=- peer=- actor=host-root "
                          "no_identity=tailnet_whois_failed")


@pytest.mark.parametrize("argv", [
    ["halt"], ["halt", "--reason"], ["halt", "--reason", ""], ["halt", "--reason", "   "],
    ["halt", "--reason", "x" * 501], ["halt", "--reason", "r", "--actor", "x"],
    ["halt", "--reason", "r", "extra"], ["halt", "--reason=", "x"],
])
def test_halt_needs_one_non_blank_bounded_reason(argv) -> None:
    code, journal, execute, runner = _halt(argv)
    assert code == 2 and execute.calls == [] and runner.calls == []
    assert journal == ["refused reason=halt_reason_invalid peer=-"]


@pytest.mark.parametrize("argv", [["halt", "--reason", "x" * 500], ["halt", "--reason=why"]])
def test_halt_accepts_a_reason_up_to_the_cli_limit(argv) -> None:
    code, _, execute, _ = _halt(argv)
    assert code == 0 and execute.calls[0][0][-2] == argv[-1].removeprefix("--reason=")
    assert op.HALT_REASON_MAX == 500


@pytest.mark.parametrize("returncode", [0, 1, 3])
def test_halt_exits_with_the_scripts_code(returncode, capsys) -> None:
    code, journal, _, _ = _halt(["halt", "--reason", HALT_REASON],
                                execute=FakeExecute(returncode=returncode))
    assert code == returncode and journal[-1].endswith(f"exit={returncode}")
    err = capsys.readouterr().err
    assert ("operations runbook" in err) is (returncode != 0)


def test_halt_journals_the_end_when_docker_cannot_run() -> None:
    def no_docker(argv: list[str], env: Mapping[str, str], stdin_text: str | None,
                  on_stderr: Any) -> int:
        raise FileNotFoundError(argv[0])

    code, journal, _, _ = _halt(["halt", "--reason", HALT_REASON], execute=no_docker)
    assert code == 1 and journal[-1].startswith("end command=halt")
    assert journal[-1].endswith("exit=1")


def test_halt_never_journals_the_reason() -> None:
    _, journal, _, _ = _halt(["halt", "--reason", HALT_REASON])
    assert "venue looks wrong" not in " ".join(journal)


def test_halt_dry_run_prints_the_argv_and_runs_nothing(capsys) -> None:
    env = _env(SSH_CONNECTION=None, BFX_ADMIN_TOKEN="t0kenNEVERshown")
    code, journal, execute, _ = _halt(["--dry-run", "halt", "--reason", HALT_REASON], env=env)
    assert code == 0 and execute.calls == [] and journal == []
    out = capsys.readouterr().out
    described = json.loads(out)
    assert described["argv"] == _halt_argv(HALT_REASON, "host-root")
    assert described["identity"] == "none" and "t0kenNEVERshown" not in out


def test_halt_not_root_refuses() -> None:
    journal: list[str] = []
    execute = FakeExecute()
    assert op.main(["halt", "--reason", "r"], env=_env(SSH_CONNECTION=None),
                   journal=journal.append, execute=execute, is_root=lambda: False) == 2
    assert journal == ["refused reason=not_root peer=-"] and execute.calls == []


def test_the_bot_container_is_the_app_compose_files() -> None:
    text = (ROOT / "deploy/vm/docker-compose.app.yml").read_text(encoding="utf-8")
    assert f"container_name: {op.BOT_CONTAINER}\n" in text and op.BOT_CONTAINER == "bfx-bot"


@pytest.mark.parametrize("command", [
    "status", "uncertainties", "credential", "resume", "kill", "enable", "disable",
    "bind-to-venue", "mark-not-accepted", "manual-resolution", "rotate-credential", "request",
])
def test_every_other_subcommand_still_needs_a_tailnet_peer(host, command) -> None:
    code, journal, execute, runner = _call([command, "--reason", "r"],
                                           env=_env(SSH_CONNECTION=None))
    assert code == 77 and execute.calls == [] and runner.calls == []
    assert journal == ["refused reason=not_tailnet_peer peer=-"]


# --------------------------------------------------------------------------- the halt script


class _Bot:
    """A stand-in for the bot's healthz server: records the request, answers ``status``."""

    def __init__(self, status: int) -> None:
        seen: list[tuple[str, str, str | None]] = []
        self.seen = seen

        def answer(handler: http.server.BaseHTTPRequestHandler) -> None:
            seen.append((handler.command, handler.path, handler.headers.get("Authorization")))
            body = json.dumps({"state": "HALTED", "cancel_all_complete": status == 200})
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(body.encode())

        # Built with type(): the stdlib's do_POST name is not ours to rename.
        handler = type("Handler", (http.server.BaseHTTPRequestHandler,),
                       {"do_POST": answer, "log_message": lambda *_args: None})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/admin/halt"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _run_halt_script(url: str, *, token: str | None = "t0kenNEVERshown") -> Any:
    assert op.HALT_SCRIPT.count(op.HALT_URL) == 1
    script = op.HALT_SCRIPT.replace(op.HALT_URL, url)  # the only test-side change
    env = {"PATH": "/usr/bin:/bin"}
    if token is not None:
        env["BFX_ADMIN_TOKEN"] = token
    return subprocess.run([sys.executable, "-I", "-c", script, HALT_REASON, "Will413028@github"],
                          env=env, capture_output=True, text=True, timeout=60, check=False)


def test_the_halt_script_targets_the_bots_own_healthz() -> None:
    assert op.HALT_URL == "http://127.0.0.1:8080/admin/halt"


@pytest.mark.parametrize(("status", "expected"), [
    (200, 0), (502, 3), (401, 1), (403, 1), (400, 1), (500, 1), (201, 1),
])
def test_the_halt_script_exit_protocol(status, expected) -> None:
    bot = _Bot(status)
    try:
        result = _run_halt_script(bot.url)
    finally:
        bot.close()
    assert result.returncode == expected, result.stderr
    assert json.loads(result.stdout)["state"] == "HALTED"  # the body reaches the operator
    (method, path, authorization), = bot.seen
    assert method == "POST" and authorization == "Bearer t0kenNEVERshown"
    query = urllib.parse.parse_qs(path.split("?", 1)[1])
    assert query == {"reason": [HALT_REASON], "actor": ["Will413028@github"]}
    assert "t0kenNEVERshown" not in result.stdout + result.stderr


def test_the_halt_script_without_an_answer_exits_1() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = _run_halt_script(f"http://127.0.0.1:{port}/admin/halt")
    assert result.returncode == 1 and result.stdout == ""
    assert "no answer from the bot" in result.stderr


def test_the_halt_script_without_a_token_sends_nothing() -> None:
    bot = _Bot(200)
    try:
        result = _run_halt_script(bot.url, token=None)
    finally:
        bot.close()
    assert result.returncode == 1 and bot.seen == []
    assert "BFX_ADMIN_TOKEN" in result.stderr
