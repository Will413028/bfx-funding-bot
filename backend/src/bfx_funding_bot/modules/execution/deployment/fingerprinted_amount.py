"""Amount fingerprints: the identity a funding submit carries without a cid.

Bitfinex funding offers drop the client cid, so after an ambiguous submit the
only link between our durable attempt and a venue offer is what the offer
looks like. Lending envelope D3a makes that link sharp: every submitted amount
encodes a fingerprint in its last four of eight decimals (1..9999), unique
among the symbol's non-terminal claims and unresolved attempts. An offer with
the attempt's exact amount, rate and period created after the attempt started
is then that attempt's offer, not a coincidence -- which is what lets a later
complete snapshot resolve an UNKNOWN without an operator.

The amount moves down by less than 0.0001 and never leaves the bounds the
planner sized it within. The planner chooses the fingerprint; the command gate
re-checks uniqueness under the account lock, in the transaction that writes the
intent, so two submits can never share one. The chosen Decimal travels unchanged
through the decision, the durable intent/attempt and the venue body, so every
fingerprint in the space is usable.
"""
from __future__ import annotations

from collections.abc import Collection
from decimal import ROUND_DOWN, Decimal
from hashlib import sha256

from bfx_funding_bot.modules.trading import AMOUNT_QUANTUM

FINGERPRINT_STEP = Decimal("0.0001")
FINGERPRINT_SPACE = 9999  # values 1..9999; 0 means "no fingerprint"


def fingerprint_seed(key: str) -> int:
    """Deterministic starting fingerprint for a submit identity."""
    return int.from_bytes(sha256(key.encode()).digest()[:8], "big") % FINGERPRINT_SPACE + 1


def fingerprinted(planned: Decimal, fingerprint: int) -> Decimal:
    """``planned`` rounded down to 4 decimals, carrying ``fingerprint``, never above it."""
    if not 1 <= fingerprint <= FINGERPRINT_SPACE:
        raise ValueError("fingerprint out of range")
    base = planned.quantize(FINGERPRINT_STEP, rounding=ROUND_DOWN)
    amount = base + fingerprint * AMOUNT_QUANTUM
    if amount > planned:
        amount -= FINGERPRINT_STEP
    return amount


def choose_fingerprinted_amount(
    planned: Decimal, *, seed_key: str, in_use: Collection[int], minimum: Decimal,
    maximum: Decimal | None,
) -> Decimal | None:
    """The fingerprinted amount for one submit, or None when no fingerprint fits.

    Starts at the fingerprint derived from ``seed_key`` and probes forward past
    fingerprints already in use or amounts outside ``[minimum, maximum]``.
    """
    if not planned.is_finite() or planned <= 0:
        return None
    start = fingerprint_seed(seed_key)
    for step in range(FINGERPRINT_SPACE):
        fingerprint = (start - 1 + step) % FINGERPRINT_SPACE + 1
        if fingerprint in in_use:
            continue
        amount = fingerprinted(planned, fingerprint)
        if amount < minimum or amount <= 0 or (maximum is not None and amount > maximum):
            continue
        return amount
    return None


__all__ = [
    "FINGERPRINT_SPACE",
    "choose_fingerprinted_amount",
    "fingerprint_seed",
    "fingerprinted",
]
