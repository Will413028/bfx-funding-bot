"""Shared schemas for Phase 2 backfill orchestration."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SeriesSpec:
    """Identifies one backfill series.

    For kind="candles": (symbol, timeframe, period_agg) all required.
    For kind="funding_stats": only symbol used.
    """
    kind: Literal["candles", "funding_stats"]
    symbol: str
    timeframe: str | None = None
    period_agg: str | None = None

    def label(self) -> str:
        if self.kind == "candles":
            return f"candles {self.symbol} {self.timeframe} {self.period_agg}"
        return f"funding_stats {self.symbol}"


@dataclass(frozen=True)
class BackfillStats:
    """Successful backfill result for one series."""
    spec: SeriesSpec
    pages: int
    rows: int           # total rows upserted (may include resume-from-DB duplicates)
    earliest_mts: int   # smallest mts now in DB for this series after run


@dataclass(frozen=True)
class BackfillError:
    """Failed backfill for one series — orchestrator continues with others."""
    spec: SeriesSpec
    error: str          # repr / brief summary of exception
