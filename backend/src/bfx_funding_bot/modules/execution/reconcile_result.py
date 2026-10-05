"""The outcome of one full-account venue reconcile run.

``BootRecovery`` returns it; the periodic reconcile loop reads it for its drift
report. It lives apart from boot recovery so readers do not import the legacy
event-store machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    n_claimed: int
    n_released: int
    n_failed: int
    reserved_usdt: Decimal = Decimal("0")
    realized_usdt: Decimal = Decimal("0")
    available_usdt: Decimal = Decimal("0")
    n_credits: int = 0
    reserved_drift_usdt: Decimal = Decimal("0")
    realized_drift_usdt: Decimal = Decimal("0")
    venue_offers: tuple[ActiveFundingOffer, ...] = ()
    n_unknown: int = 0
    n_matched: int = 0
    n_not_sent: int = 0
    snapshot_event_seq: int | None = None
    # Active offers no claim or attempt traces to (foreign, or a candidate of an
    # UNKNOWN not yet resolved). Nothing the bot may cancel or reprice.
    unmanaged_offer_ids: frozenset[str] = frozenset()
