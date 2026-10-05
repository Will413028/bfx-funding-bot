"""The operator's execution history under the ledger: the journal, then the legacy archive.

Plan Q4 (2026-09-29): the history is split at a fixed cutover watermark -- the ``set_at_ms``
of the ``ledger`` epoch the switch appended -- with a tagged cursor. Above it, the journal:

* an attempt (``started_at_ms``) is ``RESERVATION_INTENT``;
* its transport outcome (``completed_at_ms``) is ``RESERVATION_CLAIMED`` (ack),
  ``SUBMIT_OUTCOME_UNKNOWN`` (unknown) or ``RESERVATION_FAILED`` (rejected, not sent);
* a resolution (``resolved_at_ms``) is ``UNCERTAINTY_BOUND_TO_VENUE_OFFER``,
  ``UNCERTAINTY_MARKED_NOT_ACCEPTED`` or ``UNCERTAINTY_MANUALLY_RESOLVED``.

The seed copies legacy attempts with their legacy times, all before the watermark: they are
already in the archive and are not shown twice. Below the watermark the frozen ``event_log``
continues the history through the injected archive (``ExecutionHistory``), so one cursor walks
both.

Fills and credit ends are not journal facts; above the watermark they come from the venue's own
terminal history as the ledger's observations stored it, under the archive's names:

* an offer history row with ``terminal_kind='executed'`` is ``ORDER_FILL`` (symbol, the offer's
  ``amount_original``, its rate -- NULL for an FRR offer -- and ``venue_offer_id``), only for
  the bot's own offers, as the archive wrote fills only for offers it had claimed: the offer id
  is named for the scope by a transport outcome or a resolution (seeded attempts included, so
  an offer placed before the switch and filled after it counts); a manual, foreign or
  auto-renewed offer is not shown;
* a credit history row with ``terminal_kind='closed'`` and ``source_kind='credit'`` is
  ``CREDIT_CLOSED`` (symbol, amount, rate; no offer id, as in the archive, whose credit ends
  came from ``fcc`` credits only -- a closed loan is not shown).

Both are timed by the venue (``occurred_at_ms`` = ``mts_update``). Observations overlap, and a
fenced (non-accepted) one is stored too, so every observation of the scope is read and each venue
key -- ``venue_offer_id`` for a fill, ``(source_kind, venue_credit_id)`` for a credit end -- is
shown once, with its earliest observation's values. Rows timed before the watermark are left to
the archive, which has the legacy ones; they are dropped before the dedupe, which is the same as
after it while every observation reports one end at the same venue time.

Cursor ranks break ties within one millisecond: 0 attempt, 1 outcome, 2 resolution, 3 fill,
4 credit end. A fill's id is its ``venue_offer_id``, a credit end's ``source_kind:venue_credit_id``
(always ``credit:...``; the prefix keeps the ids cursors already carry).

Runs as the web API's role, so every statement names only granted columns (never
``normalized_payload`` or ``evidence``).
"""

from __future__ import annotations

import base64
import binascii
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, Text, case, cast, func, literal, null, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    ExecutionCursorError,
    ExecutionEventView,
    ExecutionHistory,
    ExecutionPage,
    Scope,
)
from bfx_funding_bot.modules.ledger.tables import (
    CapitalAuthorityEpochRow,
    ExecutionResolutionJournalRow,
    LedgerObservationCreditHistoryRow,
    LedgerObservationOfferHistoryRow,
    LedgerObservationRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)

_JOURNAL, _ARCHIVE = "j", "a"
_A = SubmissionAttemptJournalRow
_O = TransportOutcomeJournalRow
_R = ExecutionResolutionJournalRow
_Q = QuarantineOpeningRow
_OBS = LedgerObservationRow
_OH = LedgerObservationOfferHistoryRow
_CH = LedgerObservationCreditHistoryRow
# Tie-break rank within one millisecond; a cursor names one, so a rank never changes meaning.
_RANKS = ("0", "1", "2", "3", "4")


def _token(tag: str, body: str) -> str:
    return f"{tag}.{base64.urlsafe_b64encode(body.encode()).decode().rstrip('=')}"


def _untoken(token: str) -> tuple[str, str]:
    tag, separator, encoded = token.partition(".")
    if tag not in (_JOURNAL, _ARCHIVE) or not separator:
        raise ExecutionCursorError(token)
    try:
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ExecutionCursorError(token) from None
    return tag, body


def _journal_position(token: str, body: str) -> tuple[int, int, str]:
    at, rank, row_id = [*body.split(":", 2), "", ""][:3]
    if not (at.isascii() and at.isdigit() and rank in _RANKS and row_id):
        raise ExecutionCursorError(token)
    return int(at), int(rank), row_id


def _text(value: str) -> Any:
    return literal(value, Text)


class LedgerExecutionHistory:
    def __init__(self, archive: ExecutionHistory) -> None:
        self._archive = archive

    async def list_executions(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        before: str | None,
        limit: int,
        event_type: str | None,
    ) -> ExecutionPage:
        position: tuple[int, int, str] | None = None
        if before is not None:
            tag, body = _untoken(before)
            if tag == _ARCHIVE:
                return await self._from_archive(
                    session, scope, before=body or None, limit=limit, event_type=event_type)
            position = _journal_position(before, body)
        rows = await self._journal(session, scope, position, limit + 1, event_type)
        events = tuple(
            ExecutionEventView(
                event_key=f"{_JOURNAL}:{row.rank}:{row.id}",
                event_type=row.event_type,
                occurred_at_ms=int(row.at),
                symbol=row.symbol,
                venue_offer_id=row.venue_offer_id,
                cid=None,
                amount=format(Decimal(row.amount), "f") if row.amount is not None else None,
                rate=float(row.rate) if row.rate is not None else None,
            )
            for row in rows[:limit]
        )
        if len(rows) > limit:
            last = rows[limit - 1]
            return ExecutionPage(events, _token(_JOURNAL, f"{last.at}:{last.rank}:{last.id}"))
        # The journal is exhausted: the archive fills the page and carries the cursor on.
        if len(events) == limit:
            probe = await self._archive.list_executions(
                session, scope, before=None, limit=1, event_type=event_type)
            return ExecutionPage(events, _token(_ARCHIVE, "") if probe.events else None)
        rest = await self._from_archive(
            session, scope, before=None, limit=limit - len(events), event_type=event_type)
        return ExecutionPage(events + rest.events, rest.next_before)

    async def _from_archive(
        self, session: AsyncSession, scope: Scope, *, before: str | None, limit: int,
        event_type: str | None,
    ) -> ExecutionPage:
        page = await self._archive.list_executions(
            session, scope, before=before, limit=limit, event_type=event_type)
        events = tuple(
            ExecutionEventView(
                event_key=f"{_ARCHIVE}:{event.event_key}",
                event_type=event.event_type,
                occurred_at_ms=event.occurred_at_ms,
                symbol=event.symbol,
                venue_offer_id=event.venue_offer_id,
                cid=event.cid,
                amount=event.amount,
                rate=event.rate,
            )
            for event in page.events
        )
        following = page.next_before
        return ExecutionPage(events, None if following is None else _token(_ARCHIVE, following))

    async def _journal(
        self, session: AsyncSession, scope: Scope, position: tuple[int, int, str] | None,
        limit: int, event_type: str | None,
    ) -> list[Any]:
        watermark = await session.scalar(
            select(CapitalAuthorityEpochRow.set_at_ms)
            .where(CapitalAuthorityEpochRow.authority == "ledger")
            .order_by(CapitalAuthorityEpochRow.epoch_seq.desc())
            .limit(1)
        )
        since = int(watermark or 0)
        attempts = select(
            _A.started_at_ms.label("at"),
            literal(0, Integer).label("rank"),
            cast(_A.attempt_id, Text).label("id"),
            _text("RESERVATION_INTENT").label("event_type"),
            _A.symbol.label("symbol"),
            cast(null(), Text).label("venue_offer_id"),
            _A.intended_amount.label("amount"),
            _A.match_rate.label("rate"),
        ).where(
            _A.exchange_account_id == scope.exchange_account_id,
            _A.deployment_environment == scope.deployment_environment,
            _A.started_at_ms >= since,
        )
        outcomes = select(
            _O.completed_at_ms,
            literal(1, Integer),
            cast(_O.attempt_id, Text),
            case(
                (_O.kind == "ack", _text("RESERVATION_CLAIMED")),
                (_O.kind == "unknown", _text("SUBMIT_OUTCOME_UNKNOWN")),
                else_=_text("RESERVATION_FAILED"),
            ),
            _A.symbol,
            _O.venue_offer_id,
            _A.intended_amount,
            _A.match_rate,
        ).join(_A, _A.attempt_id == _O.attempt_id).where(
            _A.exchange_account_id == scope.exchange_account_id,
            _A.deployment_environment == scope.deployment_environment,
            _O.completed_at_ms >= since,
        )
        resolutions = (
            select(
                _R.resolved_at_ms,
                literal(2, Integer),
                cast(_R.id, Text),
                case(
                    (_R.action == "bound_to_venue", _text("UNCERTAINTY_BOUND_TO_VENUE_OFFER")),
                    (_R.action == "not_accepted", _text("UNCERTAINTY_MARKED_NOT_ACCEPTED")),
                    else_=_text("UNCERTAINTY_MANUALLY_RESOLVED"),
                ),
                _R.symbol,
                _R.venue_offer_id,
                func.coalesce(_A.intended_amount, _Q.intended_amount),
                _A.match_rate,
            )
            .outerjoin(_A, _A.attempt_id == _R.attempt_id)
            .outerjoin(_Q, _Q.quarantine_id == _R.quarantine_id)
            .where(
                _R.exchange_account_id == scope.exchange_account_id,
                _R.deployment_environment == scope.deployment_environment,
                _R.resolved_at_ms >= since,
            )
        )
        history = union_all(
            attempts, outcomes, resolutions, _fills(scope, since), _credit_ends(scope, since)
        ).subquery()
        stmt = select(history)
        if event_type is not None:
            stmt = stmt.where(history.c.event_type == event_type)
        if position is not None:
            stmt = stmt.where(
                tuple_(history.c.at, history.c.rank, history.c.id)
                < tuple_(literal(position[0]), literal(position[1]), literal(position[2], Text)))
        stmt = stmt.order_by(
            history.c.at.desc(), history.c.rank.desc(), history.c.id.desc()).limit(limit)
        return list((await session.execute(stmt)).all())


def _observed(scope: Scope) -> Any:
    """Every stored observation of the scope, accepted or fenced."""
    return (
        _OBS.exchange_account_id == scope.exchange_account_id,
        _OBS.deployment_environment == scope.deployment_environment,
    )


def _own_offers(scope: Scope) -> Any:
    """The venue offer ids the scope's journal names: its outcomes and its resolutions."""
    return union_all(
        select(_O.venue_offer_id).join(_A, _A.attempt_id == _O.attempt_id).where(
            _A.exchange_account_id == scope.exchange_account_id,
            _A.deployment_environment == scope.deployment_environment,
            _O.venue_offer_id.is_not(None),
        ),
        select(_R.venue_offer_id).where(
            _R.exchange_account_id == scope.exchange_account_id,
            _R.deployment_environment == scope.deployment_environment,
            _R.venue_offer_id.is_not(None),
        ),
    )


def _fills(scope: Scope, since: int) -> Any:
    """One ``ORDER_FILL`` per executed offer of the bot's, its earliest observation's values."""
    first = (
        select(
            _OH.occurred_at_ms, _OH.venue_offer_id, _OH.symbol, _OH.amount_original, _OH.rate,
        )
        .join(_OBS, _OBS.id == _OH.observation_id)
        .where(
            *_observed(scope),
            _OH.terminal_kind == "executed",
            _OH.occurred_at_ms >= since,
            _OH.venue_offer_id.in_(_own_offers(scope)),
        )
        .distinct(_OH.venue_offer_id)
        .order_by(
            _OH.venue_offer_id, _OBS.query_finished_at_ms, _OBS.id, _OH.occurred_at_ms,
        )
        .subquery()
    )
    return select(
        first.c.occurred_at_ms,
        literal(3, Integer),
        first.c.venue_offer_id,
        _text("ORDER_FILL"),
        first.c.symbol,
        first.c.venue_offer_id,
        first.c.amount_original,
        first.c.rate,
    )


def _credit_ends(scope: Scope, since: int) -> Any:
    """One ``CREDIT_CLOSED`` per closed venue credit (not loan), its earliest observation's values."""
    first = (
        select(
            _CH.occurred_at_ms, _CH.source_kind, _CH.venue_credit_id, _CH.symbol, _CH.amount,
            _CH.rate,
        )
        .join(_OBS, _OBS.id == _CH.observation_id)
        .where(
            *_observed(scope),
            _CH.terminal_kind == "closed",
            _CH.source_kind == "credit",
            _CH.occurred_at_ms >= since,
        )
        .distinct(_CH.source_kind, _CH.venue_credit_id)
        .order_by(
            _CH.source_kind, _CH.venue_credit_id, _OBS.query_finished_at_ms, _OBS.id,
            _CH.occurred_at_ms,
        )
        .subquery()
    )
    return select(
        first.c.occurred_at_ms,
        literal(4, Integer),
        first.c.source_kind + _text(":") + first.c.venue_credit_id,
        _text("CREDIT_CLOSED"),
        first.c.symbol,
        cast(null(), Text),
        first.c.amount,
        first.c.rate,
    )
