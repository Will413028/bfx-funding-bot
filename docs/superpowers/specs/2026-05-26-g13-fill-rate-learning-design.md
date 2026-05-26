# G13 — Historical Fill Rate Learning (Design)

**Date:** 2026-05-26
**Status:** Approved design → ready for implementation plan
**ROADMAP:** Phase G, G13 (數據驅動定價取代靜態常數). Last remaining strategy feature.

## 1. Problem

All fill-related pricing assumptions are static guesses with no feedback loop. The
only fill model in the codebase is the backtest engine's synthetic linear curve
`compute_fill_prob(spread_pct, fill_alpha=5.0)` (`modules/backtest/config.py`), an
arbitrary constant that drives strategy selection / WFO. We want an **empirically
learned** fill model: per rate bucket, the actual fill probability and time-to-fill,
derived from data we already have.

## 2. Key constraints (what data actually exists)

- **Available:** `funding_candles` — OHLC of **executed funding trade rates**
  (`trade:1h:f{symbol}:{period_agg}` candles), per (symbol, timeframe, period_agg),
  accumulated continuously in shadow (~weeks) + REST backfill (1–2 years).
  `funding_stats.frr` (FRR snapshots, offline backfill only).
- **NOT available:** order book depth / bestAsk / ticker / funding trades feed /
  our own real fills (canary idle → zero fills; shadow uses paper executor → no real
  venue fills).

This rules out an order-book queue model (would require new ticker/book ingestion —
separate scope, overlaps unimplemented S1/G16) and an own-fills feedback loop
(cold-start empty). It points at a **candle path-crossing** model, which is buildable
now and grounded in the matching mechanics.

## 3. Approach (chosen: A — candle path-crossing empirical model)

### 3.1 Mechanics grounding

Bitfinex funding offers are sorted **ascending by rate** and matched **FIFO**;
borrowers consume the cheapest offers first, so as demand drains the book the executed
rate climbs. Because `funding_candles` are **trade candles (executed rates)**:

> An offer resting at rate `R` would have filled within a window iff the executed-rate
> path reaches `R` in that window — i.e. `max(candle.high) ≥ R`.

This is the standard limit-order "price-crossing" fill technique used when L2/queue
data is unavailable. It is a **proxy** (ignores queue position, depth ahead, partial
fills, the 15% fee) — the best available now, and extensible (see §8).

### 3.2 Reference rate & spread buckets

- **Reference rate** at a decision time `t`: `ref = candle.close(t)` (the live-available
  "current market rate"; matches the backtest's `market_rate_source="candle_close"`).
- **Spread** of an offer: `spread = (offer_rate - ref) / ref`, stored as integer
  **basis points of relative spread** (`1% = 100 bps`). This matches the engine's
  existing `spread_pct = (decision.rate - market_rate) / market_rate`.
- **Bucket grid** (non-uniform, denser near 0; tunable module constant):
  `[-500, -200, -100, -50, 0, 50, 100, 200, 300, 500, 1000, 2000]` bps (−5%…+20%).
  Each bucket value is its **midpoint**. The learner does not bin observed spreads — it
  evaluates a *hypothetical* offer placed at each bucket midpoint (`offer = ref*(1+bps/1e4)`)
  against the realized rate path (§3.4). The model then interpolates the resulting
  discrete fill curve at query time (§3.5).

### 3.3 Horizons

Learn a small tunable set: `HORIZONS_H = [1, 4, 24]` hours (short re-price / medium /
"within a day"). Time-to-fill resolution = the candle timeframe (e.g. 1h).

### 3.4 Learning algorithm (`FillRateLearner`)

For one (symbol, timeframe, period_agg) candle series sorted by `mts`:

```
for each candle at time t with ref = close(t) (skip if ref is None or 0):
    for H in HORIZONS_H:
        window = candles with mts in (t, t+H]            # by actual mts, gap-safe
        if window is empty: continue                      # no data to decide fill
        for bucket_bps in BUCKET_GRID:
            offer = ref * (1 + bucket_bps / 1e4)
            # path-crossing: first window candle whose high >= offer
            hit = first candle c in window (mts order) with c.high >= offer
            sample = (filled = hit is not None,
                      ttf_ms = hit.mts - t if hit else None)
            accumulate sample into bucket aggregator[(H, bucket_bps)]

for each (H, bucket_bps) aggregator:
    fill_prob   = filled_count / total_count
    n_samples   = total_count
    ttf_p50_ms  = median(ttf among filled)   # None if filled_count == 0
    ttf_p90_ms  = p90(ttf among filled)
    mean_ttf_ms = mean(ttf among filled)
    write row to fill_rate_stats (source='candle', symbol, period_agg, H, bucket_bps, ...)
```

Notes:
- Window membership uses actual `mts` (handles candle gaps; never assumes contiguity).
- `high` is the fill test field (executed-rate peak). `ttf` is candle-granular.
- The learner is **idempotent**: re-running upserts the rows for the recomputed range
  (PK = source+symbol+period_agg+horizon+bucket) and records the candle range used.

### 3.5 Query interface (`FillRateModel`)

```python
@dataclass(frozen=True)
class FillEstimate:
    fill_prob: Decimal          # [0, 1]
    expected_ttf_ms: int | None # mean_ttf of the resolved bucket(s); None if ~0 fill
    n_samples: int              # min over interpolation endpoints; confidence signal
    low_confidence: bool        # n_samples < MIN_SAMPLES

class FillRateModel:
    # Loads fill_rate_stats for (source='candle', symbol, period_agg) into memory.
    # Returns None when no rows exist for (symbol, period_agg, horizon_h) → the
    # consumer falls back (e.g. backtest → linear model).
    def estimate_fill(self, *, reference_rate, offer_rate, period_agg, horizon_h) -> FillEstimate | None:
        ...
```

- Compute `spread_bps = round((offer_rate - reference_rate) / reference_rate * 1e4)`.
- **Linear interpolation** of `fill_prob` between the two adjacent bucket midpoints;
  clamp outside the grid to the endpoint values; clamp result to `[0, 1]`.
- If either endpoint bucket has `n_samples < MIN_SAMPLES`, set `low_confidence=True`
  (consumers may fall back — see §5).
- Guards: `reference_rate` None/≤0 → raise `ValueError`; unknown (symbol, period_agg,
  horizon_h) with no rows → returns `None` (consumer falls back).

## 4. Data model — `fill_rate_stats` table (Alembic migration)

| column | type | notes |
|---|---|---|
| `source` | Text | `'candle'` now; reserves `'book'` / `'own_fill'` for future blend |
| `symbol` | Text | `fUSD`, `fUST` |
| `period_agg` | Text | `p2`, `p30`, `a30` |
| `horizon_h` | Int | 1, 4, 24 |
| `spread_bucket_bps` | Int | bucket midpoint, bps of relative spread |
| `fill_prob` | Numeric | [0,1] |
| `n_samples` | Int | windows contributing |
| `ttf_p50_ms` / `ttf_p90_ms` / `mean_ttf_ms` | BigInt \| NULL | NULL if 0 fills |
| `learned_at` | TimestampTZ | compute time |
| `candle_range_start_ms` / `candle_range_end_ms` | BigInt | provenance |

- **PK:** `(source, symbol, period_agg, horizon_h, spread_bucket_bps)`.
- **NOT scoped by `deployment_environment`** — derived from public market candles, not
  from our trades (same realm-agnostic treatment as `funding_candles`).
- Migration via `cd backend_py && uv run alembic revision --autogenerate` then
  `alembic upgrade head`; `alembic check` must report no drift.

## 5. Consumer #1 — backtest engine integration

Integration point: `modules/backtest/engine.py::_apply_friction(decision, candle, config)`
(currently `spread_pct = (decision.rate - market_rate)/market_rate; fill_prob =
compute_fill_prob(spread_pct, config.fill_alpha)`).

- `BacktestConfig` gains `fill_model: Literal["empirical", "linear"] = "empirical"`.
- **`empirical` (default, best practice):** look up `FillRateModel.estimate_fill(...)`
  for the candle's (symbol, period_agg) at a chosen `horizon_h` (config:
  `fill_horizon_h: int = 4`). **Automatic per-lookup fallback to the linear model**
  when the (symbol, period_agg) has no stats or the bucket is `low_confidence`
  (logged). → existing backtests with an unpopulated `fill_rate_stats` table behave
  exactly as today (linear), so they stay green; runs with learned stats use empirical.
- **`linear`:** force the legacy `compute_fill_prob` (deterministic; used by unit tests
  that assert exact numbers).
- `compute_fill_prob` and `fill_alpha` are **retained** as the fallback model.

`time_to_fill` is **learned, stored, and queryable** now (ROADMAP requires tracking it),
but **NOT yet consumed** by the backtest return model — see §8.

## 6. Components & data flow

```
modules/lending/tracking/
  fill_rate.py     # FillRateLearner (aggregation) + BUCKET_GRID / HORIZONS_H constants
  model.py         # FillRateModel + FillEstimate (query/interpolation)
  tables.py        # fill_rate_stats SQLAlchemy model
  repository.py    # load/upsert fill_rate_stats
scripts/learn_fill_rate.py   # CLI: run learner over DB candles, populate table

funding_candles (DB) ──FillRateLearner──▶ fill_rate_stats (DB)
                                               │
                                     FillRateModel.estimate_fill
                                               │
                              backtest engine (_apply_friction)   [future: live pricing]
```

The learner is an **offline batch job** (same pattern as `scripts/backfill_phase2.py`),
**not** wired into the live daemon.

## 7. Error handling / edge cases

- Insufficient / empty candle history for a (symbol, period) → skip, log warning, write
  no rows (never emit garbage stats).
- Sparse extreme buckets → `n_samples` tracked; `low_confidence` flag; consumer fallback.
- Candle gaps → window membership by actual `mts`; empty window skipped.
- `reference_rate` None/0, None candle `high`/`close` → filtered / guarded.
- `fill_prob` clamped `[0,1]`; `ttf` percentiles NULL when zero fills.

## 8. Out of scope (explicit) / future extension

- **Order book / ticker ingestion** (approach B) — separate feature; would add
  `source='book'` rows.
- **Own-fills feedback + Bayesian blend** (approach C) — `source='own_fill'` rows
  blended with `'candle'` priors once real trading produces fills.
- **Live pricing adjustment layer + feedback loop** — no live pricing-adjustment layer
  exists yet; premature on zero data.
- **time-to-fill → backtest capital-idle/return model** — `ttf` (pre-fill resting time)
  is semantically distinct from the engine's `gap_minutes` (post-fill redeploy latency);
  doing it right means a new idle-cost term + its own validation. Deferred to avoid
  conflation. `ttf` is learned/stored now so the data is ready.

## 9. Testing (TDD)

- **Learner:** synthetic candle series with known executed-rate paths
  (monotone-rising / flat / with gaps) → hand-verified `fill_prob`, `ttf_p50/p90/mean`,
  `n_samples`, correct bucket assignment. Idempotent re-run.
- **Model:** linear interpolation correctness between buckets; endpoint/`[0,1]` clamp;
  `low_confidence` below `MIN_SAMPLES`; `ValueError` on bad reference; `None`-signal on
  unknown (symbol, period_agg).
- **Backtest integration:** `empirical` reflects learned stats; auto-fallback to linear
  when stats absent → existing tests unchanged; `linear` mode deterministic.
- **Migration:** `alembic upgrade head` creates `fill_rate_stats`; `alembic check` no
  drift.
- Gate: `cd backend_py && uv run pytest -m "not integration"` + `mypy src/` + `ruff check`.
