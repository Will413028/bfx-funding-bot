"""The operator CLI's own contract: who may run it, its flags, exit codes and output.

The commands themselves run against PostgreSQL as ``bfx_operator`` in
``tests/integration/test_operator_cli.py``.
"""
from __future__ import annotations

import argparse
import io
import json
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.apps import operator_cli
from bfx_funding_bot.modules.execution.operator_commands import OperatorCommandError

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
URL = "postgresql://bfx_operator:s3cret-password@db.invalid:5432/bfx"


@pytest.fixture
def env(monkeypatch) -> dict[str, str]:
    values = {
        "DATABASE_URL": URL,
        "BFX_EXCHANGE_ACCOUNT_ID": ACCOUNT,
        "BFX_OPERATOR_USER_ID": "operator-1",
        "BFX_OPERATOR_TAILNET_USER_ID": "4182486213003443",
        "BFX_OPERATOR_TAILNET_IDENTITY": "4182486213003443",
        "BFX_DEPLOYMENT_ENV": "ci",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


class NoEngine:
    """Fails the test if the CLI ever builds an engine."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, url: str) -> Any:
        self.calls += 1
        raise AssertionError("the CLI connected")


def _main(argv: list[str]) -> tuple[int, dict[str, Any], str]:
    out, log = io.StringIO(), io.StringIO()
    code = operator_cli.main(argv, out=out, log=log)
    return code, json.loads(out.getvalue()), log.getvalue()


@pytest.mark.parametrize("change", [
    {"BFX_OPERATOR_TAILNET_IDENTITY": "999"},
    {"BFX_OPERATOR_TAILNET_IDENTITY": ""},
    {"BFX_OPERATOR_TAILNET_IDENTITY": None},
    {"BFX_OPERATOR_TAILNET_USER_ID": None},
    {"BFX_OPERATOR_TAILNET_USER_ID": "", "BFX_OPERATOR_TAILNET_IDENTITY": ""},
])
@pytest.mark.parametrize("argv", [["status"], ["kill", "--reason", "stop"]])
def test_an_unmatched_tailnet_identity_exits_77_without_connecting(
        env, monkeypatch, change, argv) -> None:
    for key, value in change.items():
        if value is None:
            monkeypatch.delenv(key)
        else:
            monkeypatch.setenv(key, value)
    engine = NoEngine()
    monkeypatch.setattr(operator_cli, "make_async_engine_from_url", engine)
    code, payload, log = _main(argv)
    assert (code, payload, log) == (77, {"error": "operator_identity_mismatch"}, "")
    assert engine.calls == 0


@pytest.mark.parametrize("missing", ["DATABASE_URL", "BFX_EXCHANGE_ACCOUNT_ID",
                                     "BFX_OPERATOR_USER_ID"])
def test_an_incomplete_operator_env_never_connects(env, monkeypatch, missing) -> None:
    monkeypatch.delenv(missing)
    engine = NoEngine()
    monkeypatch.setattr(operator_cli, "make_async_engine_from_url", engine)
    code, payload, _ = _main(["status"])
    assert (code, payload, engine.calls) == (1, {"error": "operator_env_incomplete"}, 0)


@pytest.mark.parametrize("flag", ["--actor", "--operator", "--requested-by", "--user"])
def test_no_flag_names_who_asks(flag, capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        operator_cli.build_parser().parse_args(["kill", "--reason", "x", flag, "someone"])
    assert exited.value.code == operator_cli.EXIT["usage"]
    (commands,) = [a for a in operator_cli.build_parser()._actions
                   if isinstance(a, argparse._SubParsersAction)]
    every_option = {option for parser in commands.choices.values()
                    for a in parser._actions for option in a.option_strings}
    assert not every_option & {"--actor", "--operator", "--operator-uuid", "--requested-by"}


def test_bind_and_mark_take_no_evidence_flags() -> None:
    """The evidence of bind and mark is validated but unused, so the CLI sends none."""
    parser = operator_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["mark-not-accepted", str(uuid4()), "--evidence-ref", "r",
                           "--candidate-count", "0"])


def _stub_run(monkeypatch, result, seen: list[Any] | None = None) -> None:
    async def run(args, operator, factory, *, log, sleep=None, credential=None):
        if seen is not None:
            seen.append(credential)
        if isinstance(result, BaseException):
            raise result
        return result

    class Engine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(operator_cli, "make_async_engine_from_url", lambda url: Engine())
    monkeypatch.setattr(operator_cli, "run", run)


@pytest.mark.parametrize(("result", "code", "payload"), [
    (("ok", {"state": "applied"}), 0, {"state": "applied"}),
    (("rejected", {"state": "rejected"}), 3, {"state": "rejected"}),
    (("failed", {"state": "failed"}), 4, {"state": "failed"}),
    (("timeout", {"state": "requested"}), 5, {"state": "requested"}),
    (OperatorCommandError("request_pending", kind="conflict"), 3,
     {"error": "request_pending", "kind": "conflict"}),
    (operator_cli.CliExitError("boot", {"error": "schema_head_mismatch"}), 78,
     {"error": "schema_head_mismatch"}),
])
def test_exit_codes(env, monkeypatch, result, code, payload) -> None:
    _stub_run(monkeypatch, result)
    assert _main(["kill", "--reason", "stop"])[:2] == (code, payload)


def test_an_unexpected_failure_prints_its_type_only(env, monkeypatch) -> None:
    _stub_run(monkeypatch, OSError(f"could not connect to {URL}"))
    code, payload, log = _main(["status"])
    assert (code, payload) == (1, {"error": "internal", "type": "OSError"})
    assert "s3cret" not in json.dumps(payload) + log


def test_the_output_never_carries_the_database_url(env, monkeypatch) -> None:
    _stub_run(monkeypatch, ("ok", {"trading_state": None}))
    out, log = io.StringIO(), io.StringIO()
    operator_cli.main(["status"], out=out, log=log)
    assert "s3cret" not in out.getvalue() + log.getvalue()
    assert "DATABASE_URL" not in out.getvalue()


class _AdaptedError(Exception):
    """Stands in for the driver error SQLAlchemy wraps (``DBAPIError.orig``)."""

    def __init__(self, message: str, sqlstate: str | None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _DBAPIError(Exception):
    def __init__(self, message: str, sqlstate: str | None) -> None:
        super().__init__(message)
        self.orig = _AdaptedError(message, sqlstate)


def _unavailable(cause: BaseException | None) -> OperatorCommandError:
    error = OperatorCommandError("uncertainties_unavailable", kind="unavailable")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(("cause", "extra"), [
    # A missing grant on a read path: 42501 tells it from an ordinary refusal.
    (_DBAPIError(f"permission denied for table x [parameters: {URL}]", "42501"),
     {"cause": "_DBAPIError", "sqlstate": "42501"}),
    (_DBAPIError("odd driver", "not-a-state"), {"cause": "_DBAPIError"}),
    (LookupError(f"lost {URL}"), {"cause": "LookupError"}),
    # bot_not_running is raised without a cause: nothing is added.
    (None, {}),
])
def test_an_unavailable_refusal_names_its_cause_never_its_message(
        env, monkeypatch, cause, extra) -> None:
    _stub_run(monkeypatch, _unavailable(cause))
    code, payload, log = _main(["uncertainties"])
    assert code == 3
    assert payload == {"error": "uncertainties_unavailable", "kind": "unavailable", **extra}
    assert "s3cret" not in json.dumps(payload) + log and "permission" not in json.dumps(payload)


def test_other_refusals_never_name_a_cause(env, monkeypatch) -> None:
    error = OperatorCommandError("request_pending", kind="conflict")
    error.__cause__ = LookupError("x")
    _stub_run(monkeypatch, error)
    assert _main(["kill", "--reason", "stop"])[1] == {"error": "request_pending", "kind": "conflict"}


def test_the_identity_comparison_is_constant_time(monkeypatch) -> None:
    """``hmac.compare_digest``, not ``==``: the mutation of this check fails here."""
    seen = []
    real = operator_cli.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(operator_cli.hmac, "compare_digest", spy)
    operator = operator_cli.resolve_operator({
        "BFX_OPERATOR_TAILNET_IDENTITY": "1", "BFX_OPERATOR_TAILNET_USER_ID": "1",
        "BFX_OPERATOR_USER_ID": "operator-1", "DATABASE_URL": URL,
        "BFX_EXCHANGE_ACCOUNT_ID": ACCOUNT,
    })
    assert seen == [(b"1", b"1")]
    assert (operator.user_id, str(operator.exchange_account_id)) == ("operator-1", ACCOUNT)


# --- rotate-credential ------------------------------------------------------------------------

KEY, SECRET = "FAKE-KEY-0123456789", "FAKE-SECRET-not-a-real-one-0123456789"


class Stdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = False) -> None:
        super().__init__(text)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def _rotate(stdin: io.StringIO) -> tuple[int, dict[str, Any], str]:
    out, log = io.StringIO(), io.StringIO()
    code = operator_cli.main(["rotate-credential", "--reason", "scheduled", "--no-wait"],
                             out=out, log=log, stdin=stdin)
    return code, json.loads(out.getvalue()), log.getvalue()


def test_no_flag_carries_a_key_or_secret() -> None:
    (commands,) = [a for a in operator_cli.build_parser()._actions
                   if isinstance(a, argparse._SubParsersAction)]
    rotate = commands.choices["rotate-credential"]
    options = {o for a in rotate._actions for o in a.option_strings}
    assert options == {"-h", "--help", "--reason", "--label", "--wait-seconds", "--no-wait"}
    assert [a.dest for a in rotate._actions if not a.option_strings] == []
    with pytest.raises(SystemExit):
        operator_cli.build_parser().parse_args(
            ["rotate-credential", "--reason", "x", "--api-secret", SECRET])


def test_a_pipe_gives_two_lines_and_nothing_is_echoed(env, monkeypatch) -> None:
    seen: list[Any] = []
    _stub_run(monkeypatch, ("ok", {"state": "requested"}), seen)
    code, payload, log = _rotate(Stdin(f"{KEY}\n{SECRET}\n"))
    assert (code, payload) == (0, {"state": "requested"})
    (credential,) = seen
    assert (credential.api_key, credential.api_secret) == (KEY, SECRET)
    assert SECRET not in repr(credential) and KEY not in repr(credential)
    assert SECRET not in json.dumps(payload) + log


def test_a_terminal_is_prompted_without_echo(monkeypatch) -> None:
    prompts: list[str] = []

    def prompt(text: str) -> str:
        prompts.append(text)
        return KEY if "key" in text else SECRET

    entered = operator_cli.read_credential(Stdin(tty=True), prompt=prompt)
    assert (entered.api_key, entered.api_secret) == (KEY, SECRET)
    assert prompts == ["Bitfinex API key: ", "Bitfinex API secret: "]


@pytest.mark.parametrize("text", ["", "\n\n", f"{KEY}\n", f"{KEY}\n\n", f"\n{SECRET}\n"])
def test_a_missing_key_or_secret_is_refused_before_connecting(env, monkeypatch, text) -> None:
    engine = NoEngine()
    monkeypatch.setattr(operator_cli, "make_async_engine_from_url", engine)
    code, payload, log = _rotate(Stdin(text))
    assert (code, payload, engine.calls) == (2, {"error": "credential_input_missing"}, 0)
    assert SECRET not in json.dumps(payload) + log


def test_stdin_is_read_only_after_the_identity_check(env, monkeypatch) -> None:
    monkeypatch.setenv("BFX_OPERATOR_TAILNET_IDENTITY", "999")
    stdin = Stdin(f"{KEY}\n{SECRET}\n")
    code, payload, _ = _rotate(stdin)
    assert (code, payload) == (77, {"error": "operator_identity_mismatch"})
    assert stdin.tell() == 0
