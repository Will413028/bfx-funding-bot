"""Research composition catalog; scripts select strategies here."""
from __future__ import annotations

from bfx_funding_bot.modules.strategy import ResearchStrategySpec
from bfx_funding_bot.modules.strategy.wiring import (
    RESEARCH_STRATEGIES,
    build_strategy,
    build_strategy_at_boundary,
)

_BY_NAME = {spec.name: spec for spec in RESEARCH_STRATEGIES}


def research_strategy(name: str) -> ResearchStrategySpec:
    """Return a research spec with the original class-name report key."""
    return _BY_NAME[name]


__all__ = ["build_strategy", "build_strategy_at_boundary", "research_strategy"]
