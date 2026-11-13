"""The stored evidence an UNKNOWN attempt is matched against, and the terms it is matched by.

Read only. ``load_match_evidence`` reads the observation just accepted through the columns the
web API's role is granted; preview_terms builds an attempt's terms from its generated match
columns (advisory). The worker's authoritative terms from the payload, with its digest
rechecked, and the automatic resolver stay in ``_internal.resolver``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    Coverage,
    MatchEvidence,
    Offer,
    OfferHistory,
    OfferStatus,
    OfferTerminalKind,
    ResolutionRejected,
    Scope,
    UnknownMatch,
    UnknownTerms,
    match_unknown,
)
from bfx_funding_bot.modules.ledger.read import history_symbols
from bfx_funding_bot.modules.ledger.tables import (
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)

# Exactly the columns the web API's role is granted on the observed offers (no ``raw``): the
# matcher reads none of the venue payload, so the evidence is loaded without it.
_OFFER_COLUMNS = (
    "venue_offer_id", "symbol", "amount_original", "amount_remaining", "rate", "rate_observed",
    "period_days", "offer_type", "flags", "status", "mts_created", "mts_updated",
)


def _offer(row: Row[Any]) -> Offer:
    return Offer(
        row.venue_offer_id, row.symbol, row.amount_original, row.amount_remaining, row.rate,
        row.rate_observed, row.period_days, row.offer_type, row.flags,
        cast(OfferStatus, row.status), row.mts_created, row.mts_updated,
    )


def _page_count(counts: object, key: str) -> int:
    value = counts.get(key) if isinstance(counts, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def load_match_evidence(session: AsyncSession, observation_id: UUID) -> MatchEvidence | None:
    """The stored observation as matching evidence; None when it does not exist.

    Reads granted columns only (the generated ``history_symbols`` / ``first_page_counts``
    instead of ``evidence``, no offer ``raw``), so the web API's role can run it too.
    """
    row = (
        await session.execute(
            select(
                LedgerObservationRow.query_id,
                LedgerObservationRow.query_finished_at_ms,
                LedgerObservationRow.wallets_complete,
                LedgerObservationRow.offers_complete,
                LedgerObservationRow.credits_complete,
                LedgerObservationRow.loans_complete,
                LedgerObservationRow.offer_history_complete,
                LedgerObservationRow.credit_history_complete,
                LedgerObservationRow.trades_complete,
                LedgerObservationRow.offer_history_pages,
                LedgerObservationRow.credit_history_pages,
                LedgerObservationRow.history_requested_start_ms,
                LedgerObservationRow.history_requested_end_ms,
                LedgerObservationRow.history_oldest_mts_created,
                LedgerObservationRow.history_newest_mts_created,
                LedgerObservationRow.trades_requested_start_ms,
                LedgerObservationRow.trades_requested_end_ms,
                LedgerObservationRow.history_symbols,
                LedgerObservationRow.first_page_counts,
            )
            .where(LedgerObservationRow.id == observation_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    started_at_ms = await session.scalar(
        select(LedgerObservationQueryRow.started_at_ms).where(
            LedgerObservationQueryRow.query_id == row.query_id
        )
    )
    if started_at_ms is None:
        return None
    first = row.first_page_counts
    coverage = Coverage(
        wallets_complete=row.wallets_complete,
        offers_complete=row.offers_complete,
        credits_complete=row.credits_complete,
        loans_complete=row.loans_complete,
        offer_history_complete=row.offer_history_complete,
        credit_history_complete=row.credit_history_complete,
        wallet_pages=_page_count(first, "wallet"),
        offer_pages=_page_count(first, "offer"),
        credit_pages=_page_count(first, "credit"),
        loan_pages=_page_count(first, "loan"),
        offer_history_pages=row.offer_history_pages or 0,
        credit_history_pages=row.credit_history_pages or 0,
        history_requested_start_ms=row.history_requested_start_ms,
        history_requested_end_ms=row.history_requested_end_ms,
        history_oldest_mts_created=row.history_oldest_mts_created,
        history_newest_mts_created=row.history_newest_mts_created,
        trades_complete=row.trades_complete,
        trades_requested_start_ms=row.trades_requested_start_ms,
        trades_requested_end_ms=row.trades_requested_end_ms,
        history_symbols=history_symbols.decode({history_symbols.KEY: row.history_symbols}),
    )
    offers = await session.execute(
        select(*(getattr(LedgerObservationOfferRow, name) for name in _OFFER_COLUMNS))
        .where(LedgerObservationOfferRow.observation_id == observation_id)
        .order_by(LedgerObservationOfferRow.venue_offer_id)
    )
    history = await session.execute(
        select(
            *(getattr(LedgerObservationOfferHistoryRow, name) for name in _OFFER_COLUMNS),
            LedgerObservationOfferHistoryRow.terminal_kind,
            LedgerObservationOfferHistoryRow.occurred_at_ms,
        )
        .where(LedgerObservationOfferHistoryRow.observation_id == observation_id)
        .order_by(
            LedgerObservationOfferHistoryRow.venue_offer_id,
            LedgerObservationOfferHistoryRow.occurred_at_ms,
        )
    )
    return MatchEvidence(
        observation_id,
        started_at_ms,
        row.query_finished_at_ms,
        coverage,
        tuple(_offer(item) for item in offers),
        tuple(
            OfferHistory(
                _offer(item), cast(OfferTerminalKind, item.terminal_kind), item.occurred_at_ms
            )
            for item in history
        ),
    )


def build_terms(
    attempt_id: UUID, symbol: str, amount: Decimal, rate: Decimal, period: object,
    offer_type: object, flags: object, started_at_ms: int, unknown_recorded_at_ms: int,
) -> UnknownTerms | None:
    """The shared admission rule of an attempt's terms (None: not a matchable subject)."""
    if (
        not isinstance(period, int)
        or isinstance(period, bool)
        or period <= 0
        or not isinstance(offer_type, str)
        or not offer_type
        or not isinstance(flags, (dict, int))
        or isinstance(flags, bool)
        or not amount.is_finite()
        or amount < 0
        or not rate.is_finite()
        or started_at_ms < 0
    ):
        return None
    return UnknownTerms(
        attempt_id, symbol, amount, rate, period, offer_type, flags, started_at_ms,
        unknown_recorded_at_ms,
    )


def preview_terms(
    attempt_id: UUID, symbol: str, amount: Decimal, rate: Decimal | None, period: int | None,
    offer_type: str | None, flags: object, started_at_ms: int, unknown_recorded_at_ms: int,
) -> UnknownTerms | None:
    """The attempt's terms from the generated match columns: no payload, hence no digest recheck.

    Advisory only (the web API's preview, the CLI's queue-time check); the worker re-judges every
    request from the payload through ``_terms``. A NULL column is an absent payload key.
    """
    if rate is None or period is None or offer_type is None or flags is None:
        return None
    return build_terms(
        attempt_id, symbol, amount, rate, period, offer_type, flags, started_at_ms,
        unknown_recorded_at_ms,
    )




async def preview_match(
    session: AsyncSession, scope: Scope, attempt_id: UUID, observation: UUID
) -> UnknownMatch | None:
    """``attempt_match`` from granted columns only, so the web API's role can run it.

    The terms come from the generated match columns and the evidence from the granted
    observation columns: the payload digest is NOT rechecked and the payload's own symbol
    is not compared. Advisory (the operator's context and the queue-time check); the
    worker's ``attempt_match`` decides.
    """
    row = (
        await session.execute(
            select(
                SubmissionAttemptJournalRow.attempt_id,
                SubmissionAttemptJournalRow.symbol,
                SubmissionAttemptJournalRow.intended_amount,
                SubmissionAttemptJournalRow.match_rate,
                SubmissionAttemptJournalRow.match_period_days,
                SubmissionAttemptJournalRow.match_offer_type,
                SubmissionAttemptJournalRow.match_flags,
                SubmissionAttemptJournalRow.started_at_ms,
                TransportOutcomeJournalRow.completed_at_ms,
            )
            .join(
                TransportOutcomeJournalRow,
                TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
            )
            .where(
                SubmissionAttemptJournalRow.attempt_id == attempt_id,
                SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
                TransportOutcomeJournalRow.kind == "unknown",
            )
        )
    ).one_or_none()
    if row is None:
        return None
    terms = preview_terms(*row)
    if terms is None:
        return None
    evidence = await load_match_evidence(session, observation)
    if evidence is None:
        raise ResolutionRejected("stale_reconcile_fence")
    return match_unknown(terms, evidence)


__all__ = [
    "build_terms",
    "load_match_evidence",
    "preview_match",
    "preview_terms",
]
