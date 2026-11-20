from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler
from pydantic_core import core_schema

from bfx_funding_bot.modules.backtest.incomplete import BacktestIncompleteReason


class _AllowInfDecimal:
    """Pydantic type annotation that allows Decimal('Infinity') / Decimal('-Infinity').

    Pydantic's default Decimal validator rejects non-finite values. The sortino
    field legitimately produces +inf (no downside variance observed), so we need
    a passthrough validator that skips the finite-number check.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> object:
        def _validate(v: object) -> Decimal:
            d = v if isinstance(v, Decimal) else Decimal(str(v))
            if d.is_nan():
                raise ValueError(f"InfDecimal does not accept NaN, got {v!r}")
            return d

        return core_schema.no_info_plain_validator_function(_validate)


InfDecimal = Annotated[Decimal, _AllowInfDecimal]


class BacktestResult(BaseModel):
    """Output of a single backtest run.

    Tracks gross (pre-fee) and net (post-fee) returns separately so callers
    can attribute the gap between gross and net to the Bitfinex 15% fee.
    """

    model_config = ConfigDict(frozen=True)

    strategy_name: str
    symbol: str
    start_mts: int
    end_mts: int
    n_candles: int
    gross_monthly_return_pct: Decimal  # before Bitfinex 15% fee
    net_monthly_return_pct: Decimal    # after fee + fill_prob + gap (what user keeps)
    max_drawdown_pct: Decimal           # tracked on net equity (conservative)
    n_trades: int
    fill_rate: Decimal                  # avg fill_prob across trades; 1.0 = always filled
    sortino: InfDecimal = Decimal("0")  # monthly-equity-sampled Sortino; 0 = untrustworthy (n<3); +inf = no downside
    model_kind: Literal["empirical", "linear-baseline", "tape-replay"] = "empirical"
    model_version: str | None = None
    artifact_hash: str | None = None
    model_cutoff_ms: int | None = None
    model_sample_count: int | None = None
    incomplete_reason: BacktestIncompleteReason | None = None
    # Which market series priced each fill when `market_series_by_agg` was used
    # (key -> trade count); None for single-series runs.
    pricing_series_used: dict[str, int] | None = None
    # series key -> artifact hash when per-series fill models priced the run.
    fill_models_by_series: dict[str, str] | None = None
    # tape-replay only: the bound that booked fills, and the hours capital sat
    # idle (waiting for a fill, plus the unfilled share of each lock).
    replay_bound: Literal["conservative", "optimistic"] | None = None
    idle_hours: Decimal | None = None
    # Mean filled fraction under the same-rate queue estimate, over the offers a
    # queue provider covered (book snapshots); None when none were covered.
    queue_fill_rate: Decimal | None = None
