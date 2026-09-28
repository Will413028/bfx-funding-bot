"""Object construction only. S0-2a has no runtime composition root."""

from bfx_funding_bot.modules.trading_shadow._internal.loader import CandidateLoader
from bfx_funding_bot.modules.trading_shadow.contracts import ScanLimits

DEFAULT_SCAN_LIMITS = ScanLimits()


def build_candidate_loader(
    *, source_revision: str, limits: ScanLimits = DEFAULT_SCAN_LIMITS
) -> CandidateLoader:
    return CandidateLoader(source_revision=source_revision, limits=limits)
