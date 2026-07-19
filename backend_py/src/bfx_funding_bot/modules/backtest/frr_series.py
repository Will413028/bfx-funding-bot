"""As-of FRR lookup series for backtest arms (research-gated, no live path).

funding_stats.frr is stored as (per-day FRR rate) / 365 — ~1e-6 scale, NOT
directly comparable to funding_candles.close (~1e-4..1e-3 per-day). The
conversion frr x 365 ~= ticker FRR (per-day) was verified live on 2026-07-06
(<0.5% error, both symbols; see modules/live_validation/live_attribution.py)
and re-verified across the full 2016-2026 history on 2026-07-19 (per-year
median close/(frr*365) in [0.33, 0.97] — same order of magnitude every year,
no unit regime break). FRR_ANNUALIZATION is imported from live_attribution as
the single source of truth for that factor.
"""
from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal

from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.live_validation.live_attribution import FRR_ANNUALIZATION

_DEFAULT_MAX_STALENESS_MS = 24 * 3_600_000  # max observed gap is ~9.2h (2026-07-19 audit)


class FrrSeries:
    """Point-in-time FRR lookup: latest funding_stats row at-or-before mts.

    `at(mts)` returns the per-day FRR rate (stored frr x 365), or None when
    there is no point at-or-before mts, or the latest point is staler than
    `max_staleness_ms` (coverage gap -> arm goes idle rather than using a
    stale rate).
    """

    def __init__(
        self,
        points: list[tuple[int, Decimal]],
        *,
        max_staleness_ms: int = _DEFAULT_MAX_STALENESS_MS,
    ) -> None:
        self._points = sorted(points, key=lambda p: p[0])
        self._mts = [p[0] for p in self._points]
        self._max_staleness_ms = max_staleness_ms

    @classmethod
    def from_stats(
        cls,
        stats: list[FundingStat],
        *,
        max_staleness_ms: int = _DEFAULT_MAX_STALENESS_MS,
    ) -> FrrSeries:
        """Build from funding_stats rows, skipping null-frr rows."""
        return cls(
            [(s.mts, s.frr) for s in stats if s.frr is not None],
            max_staleness_ms=max_staleness_ms,
        )

    def __len__(self) -> int:
        return len(self._points)

    def at(self, mts: int) -> Decimal | None:
        """Per-day FRR rate as-of `mts` (stored frr x 365), or None."""
        idx = bisect_right(self._mts, mts) - 1
        if idx < 0:
            return None
        point_mts, frr_raw = self._points[idx]
        if mts - point_mts > self._max_staleness_ms:
            return None
        return frr_raw * FRR_ANNUALIZATION
