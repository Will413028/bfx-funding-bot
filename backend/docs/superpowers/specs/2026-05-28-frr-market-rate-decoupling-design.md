# FRR / market-rate decoupling — design

**Date:** 2026-05-28
**Status:** approved (brainstorming)
**Supersedes premise of:** Pending item "FRR 單位確認（market_rate_source='frr' 解出單位前不可通電）"

## Problem

The wiki Pending framed FRR as a blocker: `market_rate_source="frr"` must be
"unit-resolved" before the strategy layer can use it. Phase 3a Gate 1 already
tried to find a stable `close ~ a·frr` conversion across 5 hypotheses and
FAILED (per-year slope swung ~9×; methodology drift), then fell back to
`candle_close` and deferred to Phase 3c.

This design records the empirical investigation that resolves the blocker by
showing its premise is a category error, and decouples the two quantities.

## Evidence (Neon `lingering-resonance-64910611`, fUSD, 2016–2026)

`funding_stats.frr`: median **1.07e-6**, range 1.4e-7 … 9.75e-6 (83,904 rows).
`funding_candles.close` (1h, p2): median **2e-4**, range 1e-8 … 0.070 (85,471 rows).

Per-year median `close/frr`:

| yr | med_close | med_frr | close/frr |
|----|-----------|---------|-----------|
| 2016 | 6.86e-4 | 1.98e-6 | 362 |
| 2017 | 6.89e-4 | 2.15e-6 | 338 |
| 2018 | 1.62e-4 | 5.7e-7  | 299 |
| 2019 | 3.07e-4 | 1.02e-6 | 329 |
| 2020 | 3.15e-4 | 1.18e-6 | 308 |
| 2021 | 1.37e-4 | 6.3e-7  | 232 |
| 2022 | 1.30e-4 | 8.3e-7  | 148 |
| 2023 | 1.92e-4 | 1.63e-6 | 121 |
| 2024 | 1.30e-4 | 8.8e-7  | 153 |
| 2025 | 1.59e-4 | 1.02e-6 | 158 |
| 2026 | 1.37e-4 | 1.21e-6 | 112 |

### Reading

1. **`candle_close` is the canonical per-day market funding rate, unit confirmed.**
   Median 2e-4/day = 0.02%/day ≈ 7.3%/yr; max 0.070/day matches the real
   fUSD funding spikes of the 2017/2021 bull runs. Magnitude is realistic.
2. **FRR is a different quantity, not a unit conversion of the market rate.**
   It sits 112–362× below `candle_close`, and the ratio drifts monotonically
   (362 → 112 over the decade). No constant factor fits: per-second would make
   `frr·86400` ≈ 0.1/day (~760× too big); hourly / same-unit are also rejected.
3. **Phase 3a's failure was structural, not a tuning problem.** Regressing
   `close ~ a·frr` conflates two distinct quantities; no stable `a` exists
   because the relationship is non-stationary.
4. **FRR has never been wired.** `engine._resolve_market_rate` raises
   `ValueError` for `"frr"`; the only FRR-named strategy (`AlwaysFRRStrategy`)
   actually lends at `candle.close` and documents itself as a naive end-to-end
   smoke baseline.

### Industry framing

Quant funding/lending systems use the **actual traded / marginal rate**
(order-book top or last-traded ≈ `candle_close`) as the market reference for
fill-probability and spread modeling — never a lagging amount-weighted average
like FRR. FRR is consumed only (a) as a peg target when literally submitting
FRR-pegged floating offers, or (b) as a raw, normalized feature inside
FRR-relative strategies (trend / spike) — never converted into "the market rate".

## Decision

- **`candle_close` is the market rate.** Formalize it as the sole
  `market_rate_source`; the unit is per-day decimal.
- **FRR is a native feature, not a market-rate proxy.** Future FRR-relative
  strategies (Phase 3c) consume the FRR series directly (normalized), never
  unit-converted to a market rate.
- **Drop the misleading `"frr"` option** from `market_rate_source` — offering
  it implies a supported conversion that does not and should not exist.
- **Remove the `check_frr_unit_stability` diagnostic.** It is an always-pass
  zombie check for a now-settled hypothesis; keeping it erodes trust in the
  backfill check suite. FRR feature-drift monitoring, if wanted, belongs in
  Phase 3c co-located with the FRR feature and driven by an actual consumer.

## Code changes (minimal)

1. `modules/backtest/config.py`
   - `market_rate_source: Literal["candle_close"]` (drop `"frr"`).
   - Fix the stale docstring line ("FRR proxy until Phase 2 backfills
     funding_stats") to state `candle_close` is the canonical per-day market rate.
2. `modules/backtest/engine.py`
   - `_resolve_market_rate`: keep the defensive `ValueError` for unknown
     sources; update the comment to reference this decision.
3. `modules/backfill/checks.py`
   - Remove `check_frr_unit_stability`.
4. `scripts/backfill_phase2.py`
   - Remove the import, the `fr = await check_frr_unit_stability(...)` call,
     and the `("FRR unit stability (diagnostic)", fr)` print tuple.

No production-path (daemon/live) code is touched; this is backtest-layer + a
backfill script + a config type.

## Testing

- `tests/modules/backtest/test_config.py`: keep the default-`candle_close`
  assertion; add a test documenting `candle_close` as the only supported
  `market_rate_source` (and that `_resolve_market_rate` rejects anything else
  with `ValueError`).
- Commit gate: `uv run pytest -m "not integration"` + `uv run mypy src/`
  + `uv run ruff check` all green. (mypy enforces the narrowed `Literal`.)

## Follow-up

- After ship, run `/project-decision-log` to compress this spec into a
  second-brain ADR under `wiki/projects/bfx-funding-bot/decisions/`.
- Update wiki Pending: close "FRR 單位確認"; reword the Stage 1 / Phase 3c
  FRR lines to reflect "FRR = native feature, not market-rate proxy".
