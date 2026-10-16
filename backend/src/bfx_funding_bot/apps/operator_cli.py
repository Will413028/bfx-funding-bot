"""The operator CLI: the web API's operator commands, run as the one-off ``bfx-operator`` service.

It queues the same requests the web API does (resume and kill, currency enable and disable,
the three UNKNOWN adjudications) through ``modules.execution.operator_commands`` and reads
what they are judged against; the account daemon's workers apply them, re-checking the
operator under the account lock. It connects as ``bfx_operator``, whose grants are only
those reads and the outboxes' request columns (``4ab02765a34b``).

Who asks is the tailnet identity the host wrapper resolved for this call
(``BFX_OPERATOR_TAILNET_IDENTITY``); it must equal the one ``operator.env`` names
(``BFX_OPERATOR_TAILNET_USER_ID``) before anything connects. There is no flag naming an
actor: every request is made by ``BFX_OPERATOR_USER_ID`` (D10 Q2, until 9d), and the
daemon's ``operator_authorized`` still requires that operator's admin role and TOTP.

Before any command: the database's schema head must be this image's, and the capital
authority ``ledger`` -- the same boot checks as the web API's.

Output: one JSON object on stdout; for a request, one line on stderr naming the request
and the tailnet identity (the host journal keeps it). Exit codes are :data:`EXIT`.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, TextIO
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.authority import AuthorityMismatch, require_ledger_authority
from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
from bfx_funding_bot.core.schema_head import SchemaHeadMismatch, assert_schema_head
from bfx_funding_bot.modules.execution import operator_commands as commands
from bfx_funding_bot.modules.execution.operator_commands import (
    OperatorCommandError,
    OperatorContext,
    ResolutionCommand,
    UncertaintyDetail,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ResolutionAction,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import observation_evidence_ref
from bfx_funding_bot.modules.ledger.wiring import (
    build_operator_evidence,
    build_operator_reads,
    build_operator_resolution,
    build_policy_store,
)

EXIT: Final = {
    "ok": 0,  # read, applied, or queued with --no-wait
    "internal": 1,  # unexpected failure; nothing about it is printed beyond its type
    "usage": 2,  # argparse's own
    "rejected": 3,  # refused by the command checks, or rejected by the daemon
    "failed": 4,  # the daemon could not apply it
    "timeout": 5,  # still waiting after --wait-seconds
    "identity": 77,  # the tailnet identity is missing or not the configured operator's
    "boot": 78,  # schema head or capital authority is not what this image runs on
}
DEFAULT_WAIT_SECONDS: Final = 120
POLL_SECONDS: Final = 1.0
UNCERTAINTY_LIMIT_MAX: Final = 100  # the web API's list limit


class CliExitError(Exception):
    """A controlled exit with its outcome and payload, not a fault."""

    def __init__(self, outcome: str, payload: Mapping[str, Any]) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.payload = payload


@dataclass(frozen=True, slots=True)
class Operator:
    """Who this run acts as, all from the environment."""

    tailnet_identity: str
    user_id: str
    exchange_account_id: UUID
    database_url: str


def resolve_operator(env: Mapping[str, str]) -> Operator:
    """The configured operator, once the wrapper's tailnet identity matches it."""
    identity = env.get("BFX_OPERATOR_TAILNET_IDENTITY", "").strip()
    configured = env.get("BFX_OPERATOR_TAILNET_USER_ID", "").strip()
    if not identity or not configured or not hmac.compare_digest(
            identity.encode(), configured.encode()):
        raise CliExitError("identity", {"error": "operator_identity_mismatch"})
    user_id = env.get("BFX_OPERATOR_USER_ID", "").strip()
    database_url = env.get("DATABASE_URL", "").strip()
    try:
        account = UUID(env.get("BFX_EXCHANGE_ACCOUNT_ID", "").strip())
    except ValueError:
        account = None
    if not user_id or not database_url or account is None:
        raise CliExitError("internal", {"error": "operator_env_incomplete"})
    return Operator(identity, user_id, account, database_url)


# --- rendering ------------------------------------------------------------------------------

def resolution_request_view(row: UncertaintyResolutionRequestRow) -> dict[str, Any]:
    return {
        "request_id": str(row.request_id), "uncertainty_id": str(row.uncertainty_id),
        "action": row.action, "state": row.state,
        "evidence_ref": observation_evidence_ref(row.observation_id),
        "created_at_ms": row.created_at_ms, "processed_at_ms": row.processed_at_ms,
        "outcome_reason": row.outcome_reason,
    }


def uncertainty_view(detail: UncertaintyDetail) -> dict[str, Any]:
    """The bounded view the web API shows: an evidence summary, never the raw evidence."""
    row, context = detail.view, detail.context
    return {
        "uncertainty_id": str(row.uncertainty_id), "kind": row.kind, "symbol": row.symbol,
        "intended_amount": str(Decimal(str(row.intended_amount))), "state": row.state,
        "evidence_summary": commands.evidence_summary(row.evidence),
        "resolved_by_operator_id": row.resolved_by_operator_id,
        "resolution_reason": row.resolution_reason,
        "resolution_context": {
            "evidence_ref": context.evidence_ref,
            "query_started_at_ms": context.query_started_at_ms,
            "query_finished_at_ms": context.query_finished_at_ms,
            "candidate_count": context.candidate_count,
            "candidate_venue_offer_ids": list(context.candidate_venue_offer_ids),
            "unavailable_reason": context.unavailable_reason,
        },
        "resolution_request": (None if detail.latest_request is None
                               else resolution_request_view(detail.latest_request)),
    }


# --- commands -------------------------------------------------------------------------------

Session = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class Queued:
    """A request now in its outbox, and how to read it back."""

    request_id: UUID
    kind: str  # trading | currency | resolution
    action: str


async def _read_request(session: AsyncSession, context: OperatorContext, kind: str,
                        request_id: UUID) -> dict[str, Any]:
    if kind == "trading":
        return commands.trading_request_view(
            await commands.get_trading_request(session, context, request_id))
    if kind == "currency":
        return commands.currency_request_view(
            await commands.get_currency_request(session, context, request_id))
    return resolution_request_view(
        await commands.get_resolution_request(session, context, request_id))


async def _queue(session: AsyncSession, context: OperatorContext,
                 args: argparse.Namespace) -> Queued:
    now_ms = int(time.time() * 1000)
    if args.command in {"resume", "kill"}:
        request_id = await commands.request_trading_control(
            session, context, action=args.command, reason=args.reason, now_ms=now_ms)
        return Queued(request_id, "trading", args.command)
    if args.command in {"enable", "disable"}:
        request_id = await commands.request_currency(
            session, context, symbol=args.symbol, action=args.command, reason=args.reason,
            now_ms=now_ms)
        return Queued(request_id, "currency", args.command)
    action: ResolutionAction = args.command.replace("-", "_")
    row = await commands.request_resolution(
        session, context, build_operator_resolution(),
        ResolutionCommand(
            uncertainty_id=args.uncertainty_id, action=action, evidence_ref=args.evidence_ref,
            operator_uuid=context.user_id, reason=args.reason,
            evidence={"decision": args.decision} if action == "manual_resolution" else {},
            venue_offer_id=getattr(args, "venue_offer_id", None),
        ),
        now_ms=now_ms)
    return Queued(row.request_id, "resolution", action)


async def _read(session: AsyncSession, context: OperatorContext,
                args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "status":
        return await commands.trading_overview(
            session, context, build_policy_store(context.scope))
    if args.command == "uncertainties":
        details = await commands.list_uncertainties(
            session, context, build_operator_reads(), build_operator_evidence(),
            state=args.state, limit=args.limit)
        return {"uncertainties": [uncertainty_view(d) for d in details]}
    if args.command == "uncertainty":
        return uncertainty_view(await commands.get_uncertainty(
            session, context, build_operator_reads(), build_operator_evidence(),
            args.uncertainty_id))
    return await _read_request(session, context, args.kind, args.request_id)


WRITES: Final = frozenset({"resume", "kill", "enable", "disable", "bind-to-venue",
                           "mark-not-accepted", "manual-resolution"})
_TERMINAL_EXIT: Final = {"applied": "ok", "rejected": "rejected", "failed": "failed"}


async def _boot(session: AsyncSession) -> None:
    try:
        await assert_schema_head(session)
        await require_ledger_authority(session)
    except (SchemaHeadMismatch, AuthorityMismatch) as exc:
        raise CliExitError("boot", {"error": str(exc).split(" ", 1)[0]}) from exc


async def run(args: argparse.Namespace, operator: Operator, factory: Session, *,
              log: TextIO, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
              ) -> tuple[str, dict[str, Any]]:
    """One command; returns the outcome name (a key of :data:`EXIT`) and the JSON payload."""
    async with factory() as session:
        await _boot(session)
        context = await commands.resolve_context(
            session, exchange_account_id=operator.exchange_account_id, user_id=operator.user_id)
        if args.command not in WRITES:
            return "ok", await _read(session, context, args)
        queued = await _queue(session, context, args)
        await session.commit()
    print(f"operator_request request_id={queued.request_id} action={queued.action} "
          f"tailnet_identity={operator.tailnet_identity}", file=log, flush=True)
    deadline = time.monotonic() + (0 if args.no_wait else args.wait_seconds)
    while True:
        async with factory() as session:
            view = await _read_request(session, context, queued.kind, queued.request_id)
        if view["state"] in _TERMINAL_EXIT:
            return _TERMINAL_EXIT[view["state"]], view
        if args.no_wait:
            return "ok", view
        if time.monotonic() >= deadline:
            return "timeout", view
        await sleep(POLL_SECONDS)


# --- parser ---------------------------------------------------------------------------------

def _limit(value: str) -> int:
    limit = int(value)
    if not 1 <= limit <= UNCERTAINTY_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"must be 1..{UNCERTAINTY_LIMIT_MAX}")
    return limit


def _wait(value: str) -> int:
    seconds = int(value)
    if not 0 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("must be 0..3600")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bfx-operator", description="Operator requests for the configured account.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="trading state, cancel-all, latest requests, currencies")
    listing = sub.add_parser("uncertainties", help="the account's uncertainties")
    listing.add_argument("--state", choices=("open", "resolved"))
    listing.add_argument("--limit", type=_limit, default=50)
    one = sub.add_parser("uncertainty", help="one uncertainty and its newest request")
    one.add_argument("uncertainty_id", type=UUID)
    request = sub.add_parser("request", help="one request and its outcome")
    request.add_argument("kind", choices=("trading", "currency", "resolution"))
    request.add_argument("request_id", type=UUID)

    def write(name: str, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        wait = command.add_mutually_exclusive_group()
        wait.add_argument("--wait-seconds", type=_wait, default=DEFAULT_WAIT_SECONDS)
        wait.add_argument("--no-wait", action="store_true")
        return command

    for name in ("resume", "kill"):
        write(name, f"request a {name}").add_argument("--reason", required=True)
    for name in ("enable", "disable"):
        command = write(name, f"request to {name} one currency")
        command.add_argument("symbol")
        command.add_argument("--reason", required=True)
    bind = write("bind-to-venue", "adjudicate: the attempt is this venue offer")
    bind.add_argument("uncertainty_id", type=UUID)
    bind.add_argument("--evidence-ref", required=True)
    bind.add_argument("--venue-offer-id", required=True)
    bind.add_argument("--reason")
    mark = write("mark-not-accepted", "adjudicate: the venue never accepted the attempt")
    mark.add_argument("uncertainty_id", type=UUID)
    mark.add_argument("--evidence-ref", required=True)
    mark.add_argument("--reason")
    manual = write("manual-resolution", "adjudicate: an operator decision")
    manual.add_argument("uncertainty_id", type=UUID)
    manual.add_argument("--evidence-ref", required=True)
    manual.add_argument("--decision", required=True)
    manual.add_argument("--reason", required=True)
    return parser


async def _main(args: argparse.Namespace, operator: Operator, log: TextIO,
                ) -> tuple[str, dict[str, Any]]:
    engine = make_async_engine_from_url(operator.database_url)
    try:
        return await run(args, operator, make_session_factory(engine), log=log)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None,
         log: TextIO | None = None) -> int:
    out = out or sys.stdout
    log = log or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        operator = resolve_operator(os.environ)
        outcome, payload = asyncio.run(_main(args, operator, log))
    except CliExitError as exc:
        outcome, payload = exc.outcome, dict(exc.payload)
    except OperatorCommandError as exc:
        outcome, payload = "rejected", {"error": exc.code, "kind": exc.kind}
    except Exception as exc:
        # One bounded line: the message of a connection error may carry the URL.
        outcome, payload = "internal", {"error": "internal", "type": type(exc).__name__}
    print(json.dumps(payload, sort_keys=True, default=str), file=out, flush=True)
    return EXIT[outcome]


if __name__ == "__main__":
    raise SystemExit(main())
