"""Deterministic Bitfinex cid generator.

cid = int(blake2b(correlation_id_bytes + utc_date_iso_bytes, digest_size=8)) & 0x7FFF_FFFF_FFFF_FFFF

Bitfinex cid is a positive int with daily uniqueness window. Hash includes
the UTC date so cross-day reuse generates a fresh cid naturally.

CRITICAL: Date must be captured ONCE at executor.submit() entry, not regenerated
per retry attempt — see spec CC2 (midnight boundary race).
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
        submit_date: UTC date captured at executor.submit() entry — immutable across
                     tenacity retries (CC2 midnight race fix).

    Returns:
        Positive int63 cid suitable for Bitfinex offer/new cid field.
    """
    digest = hashlib.blake2b(
        correlation_id.bytes + submit_date.isoformat().encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") & BITFINEX_CID_MAX
