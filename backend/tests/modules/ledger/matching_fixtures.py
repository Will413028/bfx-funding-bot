"""Shared builders for the neutral UNKNOWN matcher tests (pure; no database)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

from bfx_funding_bot.modules.ledger import (
    Coverage,
    MatchEvidence,
    Offer,
    OfferHistory,
    UnknownTerms,
)

ATTEMPT_ID = UUID("22222222-2222-2222-2222-222222222222")
OBSERVATION_ID = UUID("33333333-3333-3333-3333-333333333333")
STARTED = 1_000_000  # the submit started
UNKNOWN_AT = 1_050_000  # it became UNKNOWN
QUERY_STARTED = 1_200_000  # >= STARTED + 120 s settle, > UNKNOWN_AT
QUERY_FINISHED = 1_200_500
HISTORY_START = 940_000
HISTORY_END = 1_200_400
CREATED = 1_000_100  # an offer stamped just after the submit started


def terms(**changes: Any) -> UnknownTerms:
    base = UnknownTerms(
        ATTEMPT_ID, "fUST", Decimal("100"), Decimal("0.001"), 2, "LIMIT", 0, STARTED, UNKNOWN_AT,
    )
    return replace(base, **changes)


def offer(venue_id: str = "o-1", *, created: int | None = None, **changes: Any) -> Offer:
    if created is not None:
        changes["mts_created"] = changes["mts_updated"] = created
    base = Offer(
        venue_id, "fUST", Decimal("100"), Decimal("100"), Decimal("0.001"), True, 2, "LIMIT", 0,
        "active", CREATED, CREATED, {},
    )
    return replace(base, **changes)


def history(venue_id: str = "o-1", kind: str = "executed", **changes: Any) -> OfferHistory:
    row = offer(venue_id, **changes)
    return OfferHistory(row, kind, row.mts_updated or row.mts_created)  # type: ignore[arg-type]


def coverage(**changes: Any) -> Coverage:
    base = Coverage(
        wallets_complete=True, offers_complete=True, credits_complete=True, loans_complete=True,
        offer_history_complete=True, credit_history_complete=True,
        wallet_pages=1, offer_pages=1, credit_pages=1, loan_pages=1,
        offer_history_pages=1, credit_history_pages=1,
        history_requested_start_ms=HISTORY_START, history_requested_end_ms=HISTORY_END,
        trades_complete=True, history_symbols=frozenset({"fUST"}),
    )
    return replace(base, **changes)


def evidence(
    offers: tuple[Offer, ...] = (), past: tuple[OfferHistory, ...] = (), **changes: Any,
) -> MatchEvidence:
    base = MatchEvidence(
        OBSERVATION_ID, QUERY_STARTED, QUERY_FINISHED, coverage(), offers, past,
    )
    return replace(base, **changes)
