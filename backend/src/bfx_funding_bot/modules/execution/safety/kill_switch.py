"""Kill switch: the operator's stop -- write HALTED, then cancel every funding offer.

Lending envelope ADR 2026-09-25 D4: the operator's kill (UI request or
/admin/halt) is a venue funding cancel-all per currency, which also pulls
offers placed by hand or by Bitfinex auto-renew, needing only the writer lock
and no other guard. It is the only venue cancel-all: an automatic protection
writes HALTED alone, and the planner then pulls the managed offers by id.

Order is the contract:

1. ``HALTED`` is committed first. If that write fails nothing is sent to the
   venue -- cancelling without the stop in place would let the writer re-post
   what was just pulled.
2. Then, per currency, a durable ``requested`` row, the venue call, and one
   terminal row (``acknowledged`` / ``rejected`` / ``failed`` / ``skipped``).
   A venue failure never rolls HALTED back; the result says what did not land,
   and calling :meth:`KillSwitch.engage` again retries the cancel-all without
   writing another HALTED row.

The venue call deliberately skips the command gate: provenance, uncertainty
and trading-state checks exist to stop the bot doing something new, and
"cancel everything" must still work when those projections are what is
broken. Only the writer lock is required -- without it another process is the
writer, and two writers talking to the venue is the failure the lock prevents.

Which currencies: every configured symbol, plus any symbol with an open
uncertainty (UNKNOWN submit, orphan, unsupported exposure) or a non-terminal
offer in the venue projection. If that extra read fails, the configured
currencies are still cancelled and the failure is reported.
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    FundingCancelAllPort,
    WriterLockHandle,
)
from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
from bfx_funding_bot.modules.execution.safety.trading_state import (
    CAUSE_AUTO,
    CAUSE_OPERATOR,
    HALTED,
    TradingState,
    TradingStateRepository,
)
from bfx_funding_bot.modules.ledger import ManagedOfferReader, Scope, UncertaintyReader
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

KILL_CAUSES = frozenset({CAUSE_OPERATOR, CAUSE_AUTO})
QUIESCE_TIMEOUT_S = 30.0
_FUNDING_SYMBOL = re.compile(r"^f([A-Z0-9]{2,15})$")
_DETAIL_LIMIT = 512

Quiesce = Callable[[], AbstractAsyncContextManager[bool]]


@dataclass(frozen=True, slots=True)
class CancelAllOutcome:
    currency: str
    phase: str  # acknowledged | rejected | failed | skipped
    detail: str | None = None
    venue_status: str | None = None
    attempt_id: UUID | None = None
    recorded: bool = True


@dataclass(frozen=True, slots=True)
class KillResult:
    state: TradingState
    state_changed: bool
    cancel_all: tuple[CancelAllOutcome, ...]
    scope_error: str | None = None

    @property
    def complete(self) -> bool:
        """Every currency's cancel-all was acknowledged and the scope was fully read."""
        return self.scope_error is None and bool(self.cancel_all) and all(
            outcome.phase == "acknowledged" for outcome in self.cancel_all
        )


def funding_currency(symbol: str) -> str | None:
    """``fUST`` -> ``UST``; anything that is not a funding symbol -> None."""
    match = _FUNDING_SYMBOL.match(symbol)
    return match.group(1) if match else None


def _bounded(text: str, *, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:_DETAIL_LIMIT]


class KillSwitch:
    def __init__(
        self,
        *,
        trading_state: TradingStateRepository,
        session_factory: async_sessionmaker[AsyncSession],
        ctx: AccountContext,
        configured_symbols: Iterable[str],
        venue: FundingCancelAllPort | None,
        writer_lock: WriterLockHandle | None,
        uncertainty: UncertaintyReader,
        offers: ManagedOfferReader,
        quiesce: Quiesce | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._trading = trading_state
        self._sf = session_factory
        self._uncertainty = uncertainty
        self._offers = offers
        self._ctx = ctx
        self._configured = frozenset(configured_symbols)
        self._venue = venue
        self._writer_lock = writer_lock
        self._quiesce = quiesce
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def engage(self, *, cause: str, actor: str, reason: str,
                     when_already_halted: str = "retry",
                     operator_request_id: UUID | None = None) -> KillResult:
        """Write HALTED, then cancel every funding offer at the venue.

        ``when_already_halted``: ``"retry"`` (an operator's /admin/halt) re-runs
        the cancel-all even if HALTED was already in force -- that is how an
        incomplete kill is retried. ``"skip"`` (automatic protections) does
        nothing more when HALTED was already in force, so a condition that
        persists across reconcile ticks does not call the venue or write audit
        rows every tick; only the transition into HALTED does.

        ``operator_request_id``: the kill request (``trading_control_requests``)
        this cancel-all carries out, recorded on every audit row; None for
        /admin/halt and automatic protections. The HALTED it restates was
        written, and names the request, in the request's own transaction.
        """
        if cause not in KILL_CAUSES:
            raise ValueError(f"cause {cause!r} cannot halt trading")
        if when_already_halted not in {"retry", "skip"}:
            raise ValueError(f"when_already_halted must be retry or skip, not {when_already_halted!r}")
        # 1. The stop, durably, before anything reaches the venue. Any failure
        #    here propagates and no cancel-all is attempted.
        transition = await self._trading.transition(
            HALTED, cause=cause, actor=actor, reason=reason, now_ms=self._clock(),
        )
        halted = transition.state
        if not transition.changed and when_already_halted == "skip":
            return KillResult(state=halted, state_changed=False, cancel_all=())
        currencies, scope_error = await self._currencies()
        if scope_error is not None:
            log.critical("kill_switch_scope_incomplete account=%s error=%s",
                         self._trading.account_id, scope_error)

        # 2. The venue, only with the writer lock and a live venue adapter.
        skip = None
        if self._venue is None:
            skip = "no_live_venue"
        elif self._writer_lock is None or not await self._verify_writer_lock():
            skip = "writer_lock_not_held"
        outcomes: list[CancelAllOutcome] = []
        if skip is not None:
            for currency in currencies:
                outcomes.append(await self._record_only(halted, currency, actor, "skipped", skip,
                                                        operator_request_id))
        else:
            async with self._quiesced() as quiet:
                note = None if quiet else "in_flight_command_not_quiesced"
                for currency in currencies:
                    outcomes.append(await self._cancel_all(halted, currency, actor, note,
                                                           operator_request_id))
        result = KillResult(state=halted, state_changed=transition.changed,
                            cancel_all=tuple(outcomes), scope_error=scope_error)
        level = logging.WARNING if result.complete else logging.CRITICAL
        log.log(level, "kill_switch_engaged account=%s env=%s cause=%s state_id=%s complete=%s "
                "outcomes=%s", self._trading.account_id, self._trading.environment, cause,
                halted.id, result.complete,
                [(o.currency, o.phase, o.detail) for o in outcomes])
        alerts.emit(alerts.KILL_SWITCH_ENGAGED, complete=result.complete, cause=cause,  # T8
                    actor=actor, state_id=halted.id, scope_error=scope_error or "none",
                    not_acknowledged=[(o.currency, o.phase) for o in outcomes
                                      if o.phase != "acknowledged"])
        return result

    async def _verify_writer_lock(self) -> bool:
        assert self._writer_lock is not None
        try:
            return await self._writer_lock.verify_held()
        except Exception:
            log.exception("kill_switch_writer_lock_unverifiable")
            return False

    @contextlib.asynccontextmanager
    async def _quiesced(self) -> AsyncIterator[bool]:
        if self._quiesce is None:
            yield True
            return
        try:
            context = self._quiesce()
        except Exception:
            log.exception("kill_switch_quiesce_unavailable")
            yield False
            return
        async with context as quiet:
            yield quiet

    async def _currencies(self) -> tuple[tuple[str, ...], str | None]:
        symbols = set(self._configured)
        error = None
        try:
            scope = Scope(self._trading.account_id, self._trading.environment)
            async with self._sf() as session:
                symbols.update(record.symbol for record in
                               await self._uncertainty.list_open(session, scope))
                symbols.update(await self._offers.live_symbols(session, scope))
        except Exception as exc:
            error = f"scope_unreadable: {type(exc).__name__}"
        currencies = {currency for currency in map(funding_currency, symbols) if currency}
        unmapped = sorted(symbol for symbol in symbols if funding_currency(symbol) is None)
        if unmapped:
            error = ((error + "; ") if error else "") + "unmapped_symbols: " + ",".join(unmapped)
        return tuple(sorted(currencies)), error

    async def _cancel_all(self, halted: TradingState, currency: str, actor: str,
                          note: str | None, request: UUID | None) -> CancelAllOutcome:
        assert self._venue is not None
        attempt = uuid4()
        requested = await self._append(halted, attempt, currency, "requested", actor, None, None,
                                       request)
        detail: str | None
        status: str | None
        try:
            answer = await self._venue.cancel_all_funding_offers(currency=currency, ctx=self._ctx)
        except Exception as exc:  # recorded, then the next currency is still tried
            phase, status = "failed", None
            detail = _bounded(f"{type(exc).__name__}: {exc}", secrets=(
                self._ctx.credentials.api_key, self._ctx.credentials.api_secret))
        else:
            phase = "acknowledged" if answer.outcome == "acknowledged" else "rejected"
            status = answer.venue_status[:64] if answer.venue_status else None
            detail = _bounded(answer.text, secrets=(
                self._ctx.credentials.api_key, self._ctx.credentials.api_secret)) if answer.text else None
        if note is not None:
            detail = _bounded(f"{note}; {detail}" if detail else note)
        recorded = await self._append(halted, attempt, currency, phase, actor, status, detail,
                                      request)
        return CancelAllOutcome(currency=currency, phase=phase, detail=detail, venue_status=status,
                                attempt_id=attempt, recorded=requested and recorded)

    async def _record_only(self, halted: TradingState, currency: str, actor: str,
                           phase: str, detail: str, request: UUID | None) -> CancelAllOutcome:
        attempt = uuid4()
        recorded = await self._append(halted, attempt, currency, phase, actor, None, detail, request)
        return CancelAllOutcome(currency=currency, phase=phase, detail=detail,
                                attempt_id=attempt, recorded=recorded)

    async def _append(self, halted: TradingState, attempt: UUID, currency: str, phase: str,
                      actor: str, venue_status: str | None, detail: str | None,
                      request: UUID | None) -> bool:
        """Record one phase. A failed audit write never stops the kill itself."""
        try:
            async with self._sf.begin() as session:
                session.add(FundingCancelAllAuditRow(
                    exchange_account_id=self._trading.account_id,
                    deployment_environment=self._trading.environment,
                    trading_state_id=halted.id,
                    attempt_id=attempt,
                    currency=currency,
                    phase=phase,
                    venue_status=venue_status,
                    detail=detail,
                    actor=actor,
                    occurred_at_ms=self._clock(),
                    operator_request_id=request,
                ))
            return True
        except Exception:
            log.critical("kill_switch_audit_write_failed attempt=%s currency=%s phase=%s detail=%s",
                         attempt, currency, phase, detail, exc_info=True)
            return False


__all__ = [
    "KILL_CAUSES",
    "QUIESCE_TIMEOUT_S",
    "CancelAllOutcome",
    "KillResult",
    "KillSwitch",
    "funding_currency",
]
