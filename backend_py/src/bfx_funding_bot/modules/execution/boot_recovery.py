"""Boot-time venue reconciliation (Phase 4.4c / 3a-recovery).

The venue (Bitfinex) is the ultimate truth for which funding offers exist;
the PG event_log + snapshot is the SoT for our intent + accounting. Bitfinex
funding offers carry NO client cid (submit drops it, response lacks it), so we
reconcile by venue_offer_id -- the only stable shared key.

  - orphan  (venue has voi, local has no CLAIMED row)  -> ReservationClaimed
  - missing (local CLAIMED, venue no longer has voi)   -> ReservationReleased
  - stale PENDING (write-ahead intent, unresolvable)   -> ReservationFailed

PENDING can't be matched to the venue (no cid round-trip), so it converges to
FAILED after reconcile -- capital-neutral, because the actual offer (if the
submit reached the venue) is captured independently by orphan-claim.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid5

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.cid import BITFINEX_CID_MAX
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

RecoveryAction = ReservationClaimed | ReservationReleased | ReservationFailed

# Fixed namespace for deterministic synthetic correlation ids on reconciled
# orphans (offers with no originating local signal).
_RECOVERY_SCID_NS = UUID("3a000000-0000-4000-8000-000000000001")


@dataclass(frozen=True, slots=True)
class LocalClaim:
    cid: int
    venue_offer_id: str | None
    state: RegistryState
    size_usdt: Decimal
    signal_correlation_id: UUID
    occurred_at_ms: int


def synth_orphan_cid(venue_offer_id: str) -> int:
    """Synthetic cid for a reconciled orphan claim, in the NEGATIVE namespace.

    Real cids (generate_cid) are blake2b masked by BITFINEX_CID_MAX -> always
    positive, so negating guarantees zero collision with a client-submitted
    intent. Synthetic cids are never sent to the venue (orphans are reconciled,
    not submitted); a negative cid in event_log marks "no originating intent".
    Numeric voi (the normal case) is negated directly so re-running reconcile
    upserts the same offer_claims row; non-numeric voi falls back to a hashed
    value, also negated.
    """
    try:
        return -int(venue_offer_id)
    except ValueError:
        digest = hashlib.blake2b(venue_offer_id.encode(), digest_size=8).digest()
        return -(int.from_bytes(digest, "big") & BITFINEX_CID_MAX)


def synth_orphan_scid(venue_offer_id: str) -> UUID:
    """Deterministic correlation id for a reconciled orphan (no local signal)."""
    return uuid5(_RECOVERY_SCID_NS, venue_offer_id)


def compute_recovery_actions(
    *,
    venue_offers: list[ActiveFundingOffer],
    local_claims: list[LocalClaim],
    account_id: str,
    is_simulated: bool,
    now_ms: int,
    grace_ms: int,
) -> list[RecoveryAction]:
    """Pure reconciliation: produce the ordered list of domain events to append."""
    venue_by_voi = {o.venue_offer_id: o for o in venue_offers}
    claimed_by_voi = {
        c.venue_offer_id: c
        for c in local_claims
        if c.state == RegistryState.CLAIMED and c.venue_offer_id is not None
    }
    actions: list[RecoveryAction] = []

    # orphan: venue has it, local CLAIMED set doesn't -> claim (reserved += size)
    for voi, offer in venue_by_voi.items():
        if voi not in claimed_by_voi:
            actions.append(ReservationClaimed(
                cid=synth_orphan_cid(voi), venue_offer_id=voi,
                size_usdt=offer.amount, signal_correlation_id=synth_orphan_scid(voi),
                account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            ))

    # missing: local CLAIMED, venue gone -> release (reserved -= size)
    for voi, claim in claimed_by_voi.items():
        if voi not in venue_by_voi:
            actions.append(ReservationReleased(
                cid=claim.cid, venue_offer_id=voi, size_usdt=claim.size_usdt,
                reason="missing_from_venue", signal_correlation_id=claim.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            ))

    # stale PENDING (crash-mid-flight, unmatchable) -> FAILED (capital-neutral)
    for c in local_claims:
        if c.state == RegistryState.PENDING and (now_ms - c.occurred_at_ms) >= grace_ms:
            actions.append(ReservationFailed(
                cid=c.cid, size_usdt=c.size_usdt,
                signal_correlation_id=c.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated,
                reason="unresolved_at_boot", occurred_at_ms=now_ms,
            ))

    return actions
