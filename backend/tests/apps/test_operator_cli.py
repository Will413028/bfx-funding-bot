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


def _stub_run(monkeypatch, result) -> None:
    async def run(args, operator, factory, *, log, sleep=None):
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
