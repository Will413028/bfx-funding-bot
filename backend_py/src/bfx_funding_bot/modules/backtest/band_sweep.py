"""Pure logic for the AdaptivePeriod band (t1,t2) sweep.

Reuses the OOS engine + stats primitives; adds the two new statistics the
shared build_cell_report does not compute (active-series DSR; paired
band-vs-band difference CI). No engine/oos_eval changes. See
docs/superpowers/specs/2026-06-04-adaptive-period-band-sweep-design.md.
"""
from __future__ import annotations

from decimal import Decimal

_T1_VALUES = [Decimal("0.5"), Decimal("1.0"), Decimal("1.5")]
_T2_VALUES = [Decimal("1.5"), Decimal("2.0"), Decimal("2.5")]


def enumerate_bands() -> list[tuple[Decimal, Decimal]]:
    """The 8 (t1, t2) variants, strict t1 < t2 (drops the (1.5,1.5) cell)."""
    return [(t1, t2) for t1 in _T1_VALUES for t2 in _T2_VALUES if t1 < t2]
