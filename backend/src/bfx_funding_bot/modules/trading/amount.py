"""The venue wire-amount codec: which fingerprint an amount carries (D3a).

Pure and shared: the legacy planner and command gate and the ledger's offer
reader must agree on this mapping, and neither may import the other.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

AMOUNT_QUANTUM = Decimal("0.00000001")


def fingerprint_of(amount: object) -> int | None:
    """The fingerprint an amount carries, or None when it is not a valid wire amount."""
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= 0 or value != value.quantize(AMOUNT_QUANTUM):
        return None
    return int(value.scaleb(8)) % 10_000


__all__ = ["AMOUNT_QUANTUM", "fingerprint_of"]
