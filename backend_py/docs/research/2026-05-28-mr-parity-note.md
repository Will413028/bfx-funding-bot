# MR Parity Note: Backtest == Live Marketfeed (OOS Validation)

**Date:** 2026-05-28
**Branch:** feat/oos-profitability-validation
**Verdict: SEMANTICALLY EQUIVALENT — OOS backtest validation is faithful.**

---

## Context

OOS profitability validation backtests `MeanReversionStrategy` from
`backend_py/src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py`
against the deployed canary config. This note verifies that the live
marketfeed daemon uses the same decision logic — not a divergent copy.

Deployed canary cells (`backend_py/configs/cells.canary.yaml`, lines 23-34):

```yaml
# fUST a30
strategy: mean_reversion
params: {threshold_sigma: 1.0, ratio_sigma: 0.9915, ema_alpha: 0.01183}

# fUST p2
strategy: mean_reversion
params: {threshold_sigma: 1.0, ratio_sigma: 0.9543, ema_alpha: 0.01183}
```

---

## Key Finding: Single Implementation

The live marketfeed runtime does **not** have a separate MeanReversion
implementation. `strategy_registry.py` directly imports and instantiates the
same class:

```python
# backend_py/src/bfx_funding_bot/modules/marketfeed/strategy_registry.py, lines 17-18
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
```

`build_strategy()` (lines 50-66) constructs it as:

```python
ema_span = _ema_alpha_to_span(float(p["ema_alpha"]))
return MeanReversionStrategy(
    ema_span=ema_span,
    threshold_sigma=Decimal(str(p["threshold_sigma"])),
    ratio_sigma=Decimal(str(p["ratio_sigma"])),
)
```

There is no shadow/alternate MR class anywhere in the codebase. The live path
and the backtest path instantiate the exact same Python class.

---

## EMA Alpha Mapping

`cells.canary.yaml` stores `ema_alpha: 0.01183` (smoothing factor directly).
`MeanReversionStrategy.__init__` takes `ema_span: int` and computes alpha
internally as `alpha = Decimal(2) / Decimal(ema_span + 1)`.

The conversion (registry `_ema_alpha_to_span`, lines 42-47):

```python
def _ema_alpha_to_span(alpha: float) -> int:
    return max(1, round(2.0 / alpha - 1))
```

For `ema_alpha = 0.01183`:

```
span_exact  = 2 / 0.01183 - 1 = 168.0617...
span_rounded = 168
alpha_from_span = 2 / (168 + 1) = 2 / 169 = 0.011834319...
```

Absolute difference: `|0.01183 - 0.011834...| ≈ 4.3e-6` (~0.037% relative).
This is a rounding artefact of storing the rounded span (168), not a
semantic difference. For any realistic funding-rate data, this difference is
orders of magnitude below the band width
(`threshold_sigma * ratio_sigma = 1.0 * 0.9915 = 0.9915`) and cannot flip
a lend/pause decision.

---

## Decision Rule Comparison

### Backtest implementation
(`backend_py/src/bfx_funding_bot/modules/backtest/strategies/mean_reversion.py`,
lines 39-57):

```python
def observe(self, candle: FundingCandle) -> None:
    if candle.close is None:
        return
    if self._ema is None:
        self._ema = candle.close          # first close seeds EMA
    else:
        self._ema = self._alpha * candle.close + (Decimal("1") - self._alpha) * self._ema

def decide(self, candle: FundingCandle) -> LendDecision | None:
    if candle.close is None or self._ema is None or self._ema == 0:
        return None
    deviation = (candle.close - self._ema) / self._ema
    lower_band = -self._threshold_sigma * self._ratio_sigma
    if deviation < lower_band:
        return None                       # PAUSE
    return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)  # LEND
```

### Live marketfeed call path

1. `ExtractedSignal.extract()` (divergence_reporter.py, lines 48-49):
   ```python
   strategy.observe(candle)
   ld = strategy.decide(candle)
   ```
2. `strategy` is the **same** `MeanReversionStrategy` instance (constructed by
   `build_strategy()` in strategy_registry.py, warmed up by `warmup.py`).

No extra filters, spike guards, or rate-field substitutions occur between
`extract()` and the `strategy.decide()` call. The safety chain
(`_apply_safety_eval`, signal_engine.py line 281) runs post-strategy and can
downgrade POST→SKIP for risk reasons, but does not alter the MR decision
logic itself.

---

## Comparison Summary

| Dimension | Backtest | Live Marketfeed | Equivalent? |
|---|---|---|---|
| MR class | `MeanReversionStrategy` (backtest module) | Same class (imported directly) | **Yes — identical code** |
| EMA recursion | `alpha * close + (1-alpha) * ema` | Same (same class) | **Yes** |
| EMA seeding | First `close` seeds `_ema` | Same (same class) | **Yes** |
| Alpha value | `2/(168+1) = 0.011834...` | `round(2/0.01183-1)=168` → same span | **Yes, within 0.037%** |
| Band formula | `-threshold_sigma * ratio_sigma` | Same (same class) | **Yes** |
| Comparison | `deviation < lower_band` → pause | Same (same class) | **Yes** |
| Period | `period_days=2` | Same (same class) | **Yes** |
| Extra filters | None | None in MR path | **Yes** |
| EMA warmup | Backtest observes history candles | `build_strategy_at_boundary` observes history[:-1] | **Symmetric by design** |

---

## Conclusion

The backtest `MeanReversionStrategy` **is semantically equivalent** to the
live marketfeed MeanReversion for the deployed canary params. There is no
separate implementation — the live path imports and runs the same class. The
only numerical difference is the alpha rounding artefact (~0.037%), which
cannot flip any lend/pause decision on realistic funding-rate data given
the band width of ~0.99 sigma.

**OOS profitability validation using the backtest is faithful to what the
deployed canary daemon actually executes.**
