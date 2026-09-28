"""Checked by mypy from test_contracts; assignments require structural conformance."""
from decimal import Decimal

from bfx_funding_bot.modules.strategy import (
    AdaptivePeriodStrategy,
    AlwaysFrrStrategy,
    AlwaysMarketRateStrategy,
    CellStrategyFactory,
    MeanReversionFrrFloorStrategy,
    MeanReversionStrategy,
    RatePercentileStrategy,
    ResearchStrategySpec,
    StrategyDiagnosticPort,
    StrategyInstance,
)
from bfx_funding_bot.modules.strategy.wiring import RESEARCH_STRATEGIES, build_strategy


def check_conformance() -> None:
    mr = MeanReversionStrategy(3, Decimal("1"), Decimal("0.05"))
    rp = RatePercentileStrategy(75, 3)
    ap = AdaptivePeriodStrategy(3, Decimal("0.05"), Decimal("0.5"), Decimal("2"), 7, 14)
    am = AlwaysMarketRateStrategy()
    af = AlwaysFrrStrategy(lambda mts: None)
    mf = MeanReversionFrrFloorStrategy(3, Decimal("1"), Decimal("0.05"), lambda mts: None)
    instances: tuple[StrategyInstance, ...] = (mr, rp, ap, am, af, mf)
    diagnostics: tuple[StrategyDiagnosticPort, ...] = (mr, rp, ap, am, af, mf)
    factory: CellStrategyFactory = build_strategy
    specs: tuple[ResearchStrategySpec, ...] = RESEARCH_STRATEGIES
    assert instances and diagnostics and factory and specs
