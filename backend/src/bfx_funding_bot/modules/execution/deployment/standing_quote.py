"""StandingQuote — per-cell standing intent produced by the signal layer.

Decoupling (spec 2026-05-29): the signal layer (1h candle boundary) decides
the *terms* (POST{rate,period} / SKIP) and writes a StandingQuote here. The
deployment reconciler (90s) reads active (POST + non-expired) quotes and
deploys idle capital toward them without recomputing the signal.

E2 (book-aware clamp, 2026-07-06): submit 前 deployment 層可在 policy 界內把
rate 對齊 live book（taker / undercut / raise；ARCHITECTURE §4 步驟 7c）。
StandingQuote.rate 仍是 POST/SKIP 閘門與 down-clamp floor 的權威 — clamp
只調執行價，不回寫 quote。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from bfx_funding_bot.modules.strategy import DecisionOutcome


@dataclass(frozen=True, slots=True)
class StandingQuote:
    cell_id: str
    outcome: DecisionOutcome          # POST / SKIP
    rate: Decimal | None              # set iff POST; exact, feeds the submit rate
    period_days: int | None           # set iff POST
    signal_correlation_id: UUID
    created_at_ms: int                # wall-clock ms when written


class StandingQuoteStore:
    """In-memory per-cell quote map. Written by signal layer, read by reconciler.

    Not persisted: on cold start it is empty and repopulates at the first candle
    boundary (matches pre-refactor behavior; DECISION forensics live in PG).
    """

    def __init__(self, *, ttl_ms: int = 3_900_000) -> None:
        self._ttl_ms = ttl_ms
        self._quotes: dict[str, StandingQuote] = {}

    def update(self, quote: StandingQuote) -> None:
        self._quotes[quote.cell_id] = quote

    def get_active(self, cell_id: str, *, now_ms: int) -> StandingQuote | None:
        """Return the quote iff it is POST and within TTL; else None."""
        q = self._quotes.get(cell_id)
        if q is None:
            return None
        if q.outcome != DecisionOutcome.POST:
            return None
        if now_ms - q.created_at_ms > self._ttl_ms:
            return None
        return q
