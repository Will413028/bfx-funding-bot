"""Pure candle-grid reindexing and LOCF values."""
from dataclasses import dataclass

from bfx_funding_bot.modules.candles.schemas import FundingCandle

_MS_PER_HOUR = 3_600_000


@dataclass(frozen=True)
class FilledCandle:
    """Wrapper for a candle slot after LOCF reindexing.

    candle is None when the slot is beyond max_gap_hours from any source candle
    (hard tier — caller must skip signal emit and emit DEGRADED instead).
    """
    candle: FundingCandle | None
    is_stale: bool
    stale_seconds: int


def reindex_and_ffill(
    candles: list[FundingCandle],
    ref_mts: int,
    freq_ms: int = _MS_PER_HOUR,
    max_gap_hours: int = 2,
) -> list[FilledCandle]:
    """Reindex sparse candle list to a full hourly grid ending at ref_mts.

    Forward-fills missing slots up to max_gap_hours of staleness. Beyond budget,
    slots remain (candle=None, is_stale=True, stale_seconds=(slot - source)/1000).

    Invariants:
    - len(output) == n hourly slots from candles[0].mts to ref_mts (or [] if candles empty)
    - output[-1] aligns to ref_mts (last slot within freq_ms tolerance)
    - For 100% dense input on freq_ms grid: 1-to-1 wrap, no ffill applied
    - Deterministic: same input → same output across N invocations
    """
    if not candles:
        return []
    sorted_candles = sorted(candles, key=lambda c: c.mts)
    if sorted_candles[-1].mts > ref_mts:
        sorted_candles = [c for c in sorted_candles if c.mts <= ref_mts]
        if not sorted_candles:
            return []

    start_mts = sorted_candles[0].mts
    n_slots = ((ref_mts - start_mts) // freq_ms) + 1
    max_gap_ms = max_gap_hours * _MS_PER_HOUR

    output: list[FilledCandle] = []
    source_idx = 0
    source = sorted_candles[0]

    for slot in range(n_slots):
        slot_mts = start_mts + slot * freq_ms
        while (
            source_idx + 1 < len(sorted_candles)
            and sorted_candles[source_idx + 1].mts <= slot_mts
        ):
            source_idx += 1
            source = sorted_candles[source_idx]

        if source.mts == slot_mts:
            output.append(FilledCandle(candle=source, is_stale=False, stale_seconds=0))
            continue

        age_ms = slot_mts - source.mts
        if age_ms <= max_gap_ms:
            output.append(
                FilledCandle(candle=source, is_stale=True, stale_seconds=age_ms // 1000)
            )
        else:
            output.append(
                FilledCandle(candle=None, is_stale=True, stale_seconds=age_ms // 1000)
            )
    return output
