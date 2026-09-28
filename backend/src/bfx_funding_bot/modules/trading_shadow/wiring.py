"""Object construction only. S0-2a has no runtime composition root."""

from functools import partial

from bfx_funding_bot.modules.trading_shadow._internal.comparison import compare_capital
from bfx_funding_bot.modules.trading_shadow._internal.loader import CandidateLoader
from bfx_funding_bot.modules.trading_shadow.contracts import (
    BaselineReader,
    CapitalComparator,
    ScanLimits,
)

DEFAULT_SCAN_LIMITS = ScanLimits()


def build_candidate_loader(
    *, source_revision: str, limits: ScanLimits = DEFAULT_SCAN_LIMITS
) -> CandidateLoader:
    return CandidateLoader(source_revision=source_revision, limits=limits)


def build_capital_comparator(
    *, source_revision: str, baseline_reader: BaselineReader,
    limits: ScanLimits = DEFAULT_SCAN_LIMITS,
) -> CapitalComparator:
    """Bind independent readers; the caller owns the snapshot and clock."""
    return partial(
        compare_capital,
        candidate_reader=build_candidate_loader(source_revision=source_revision, limits=limits),
        baseline_reader=baseline_reader,
    )
