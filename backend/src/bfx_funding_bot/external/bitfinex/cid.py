"""Deterministic internal correlation id ("cid") generator.

cid = int(blake2b(correlation_id_bytes + utc_date_iso_bytes, digest_size=8)) & 0x7FFF_FFFF_FFFF_FFFF

NOT sent to Bitfinex: the funding-offer submit API has no cid field (only trading
orders do), so this is purely an internal idempotency/correlation key. It ties a
journaled submit attempt to its outcome — see AccountCommandGate. The int63 /
positive shape mirrors Bitfinex's
historical cid format for consistency, nothing more.

Determinism: same (correlation_id, UTC date) → same cid, so a crashed submit's
PENDING intent is recoverable at boot. The UTC date gives a daily window (cross-day
reuse yields a fresh cid). CRITICAL: capture the date ONCE at submit() entry, not
per attempt — see spec CC2 (midnight boundary race).
"""
from __future__ import annotations

import hashlib
from datetime import date
from uuid import UUID

# Bitfinex cid is a signed 64-bit integer interpreted as positive only.
BITFINEX_CID_MAX = 0x7FFF_FFFF_FFFF_FFFF


def generate_cid(correlation_id: UUID, submit_date: date) -> int:
    """Generate deterministic cid for (correlation_id, submit_date) pair.

    Args:
        correlation_id: Strategy decision correlation UUID. Stable per signal cycle.
        submit_date: UTC date captured ONCE at submit() entry (CC2 midnight race fix).

    Returns:
        Positive int63 internal correlation id (NOT sent to Bitfinex — see module docstring).
    """
    digest = hashlib.blake2b(
        correlation_id.bytes + submit_date.isoformat().encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") & BITFINEX_CID_MAX
