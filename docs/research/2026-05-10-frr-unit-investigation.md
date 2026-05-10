# FRR Unit Investigation

**Date**: 2026-05-10
**Owner**: Will
**Spec**: `docs/superpowers/specs/2026-05-10-phase3a-frr-unit-investigation-design.md`
**Sample row anchor** (per `funding_stats/schemas.py` docstring):

```
mts=1778411100000, frr=1.12e-06, avg_period=94.98, candle.close (1h p2)=0.00014465
```

## 1. Problem statement

Phase 2 抽樣 `frr × 86400 / candle.close = 668.98`，推翻 5/9「FRR=秒利率」推測。
Phase 3a 走 reference-first → empirical-confirmation 流程解單位。

## 2. Stage 1 reference findings

**Stage 2 mode**: full (all 5 hypotheses tested). Stage 1 produced no
authoritative formula — bitfinex-api-py SDK and ccxt both return raw
values without unit context; Bitfinex Help Center primary URLs 403'd,
only secondary-source paraphrase of "daily rate" obtained. Insufficient
to skip to confirm-only mode. Stage 2 must regression-test all 5 H.

### 2.1 bitfinex-api-py SDK

**Source**: bitfinex-api-py v4.0.0 (downloaded 2026-05-10)
**File**: `bfxapi/types/dataclasses.py:155-161` + `bfxapi/types/serializers.py:239-256`

**Class signature** (verbatim):
```python
@dataclass
class FundingStatistic(_Type):
    mts: int
    frr: float
    avg_period: float
    funding_amount: float
    funding_amount_used: float
    funding_below_threshold: float
```

**Serializer** (verbatim):
```python
FundingStatistic = generate_labeler_serializer(
    name="FundingStatistic",
    klass=dataclasses.FundingStatistic,
    labels=[
        "mts",
        "_PLACEHOLDER",
        "_PLACEHOLDER",
        "frr",
        "avg_period",
        "_PLACEHOLDER",
        "_PLACEHOLDER",
        "funding_amount",
        "funding_amount_used",
        "_PLACEHOLDER",
        "_PLACEHOLDER",
        "funding_below_threshold",
    ],
)
```

**Unit hint** (verbatim docstring or comment about FRR units):
no unit comment found

**Derived hypothesis weight**:
- H1 (per-day): silent
- H2 (per-second): silent
- H3 (per-period × avg_period): silent
- H4 (per-period-second): silent
- H5 (annualized / 365): silent

### 2.2 ccxt python source

**Source**: ccxt master @ commit a3ce5a9a78aab2a661d7a3bdc4f67673d8ba28f2
**File**: python/ccxt/bitfinex.py
**Relevant lines**: 1176, 1191, 1296, 1311 (FRR in comments only); 3002-3050 (fetch_funding_rates); 3125-3176 (parse_funding_rate)

**FRR parser** (verbatim):
```python
# Line 1176, 1296: FRR listed in API response structure comments for funding currencies
#            FRR,
#            ...
#            FRR_AMOUNT_AVAILABLE

# Lines 3127-3152: API response structure from /v2/status/deriv endpoint
#       [
#          "tBTCF0:USTF0",
#          1691165059000,  # timestamp
#          null,
#          29297.851276225,  # indexPrice (index 3)
#          ...
#          1691193600000,  # nextFundingTimestamp (index 8)
#          0.00000527,  # nextFundingRate (index 9)
#          ...
#          0.00014548,  # fundingRate (index 12)
#          ...
#       ]

# Lines 3166, 3169: Actual extraction (NO UNIT CONVERSION)
'fundingRate': self.safe_number(contract, 12),  # 0.00014548
'nextFundingRate': self.safe_number(contract, 9),  # 0.00000527
```

**Unit hint** (extracted verbatim from parse_funding_rate):
ccxt does NOT parse FRR from the `/tickers` endpoint (only mentioned in comments as available field). For derivatives status endpoint (`/v2/status/deriv`), fundingRate and nextFundingRate are extracted directly via `safe_number()` with NO divisors, multipliers, or comments about units. Raw values: 0.00014548 (current), 0.00000527 (next). ccxt does not provide FRR-specific parsing or unit documentation.

**Derived hypothesis weight**:
- H1 (per-day): silent (ccxt provides raw number, no conversion)
- H2 (per-second): silent (no *86400 or /86400 conversion in code)
- H3 (per-period × avg_period): silent (ccxt ignores avg_period entirely)
- H4 (per-period-second): silent (no period conversion logic)
- H5 (annualized / 365): silent (no /365 conversion in code)

### 2.3 Bitfinex Help Center

**Source**: Bitfinex Help Center articles 213919009 (FRR) + 115003284729 (FRR Delta), fetched 2026-05-10 via WebFetch + WebSearch (direct Cloudflare 403; secondary sources: earn-usd.com, cryptoticker.io, blog.bitfinex.com)

**FRR definition** (from secondary Help Center summaries):
> The Flash Return Rate (FRR) is a special rate calculated based on the weighted average rate of all borrowing transactions on the Bitfinex platform. It is automatically updated every hour.

**FRR formula / calculation** (from aggregated Help Center references):
> FRR is the weighted average of all active fixed-rate fundings by amount, recalculated hourly without explicit formula stated in Help Center articles.

**Time scale** (per Help Center references, specific wording):
> "Automatically updated every hour" / "updates once per hour". Interest rates quoted as daily percentages (e.g., 0.06% daily). Interest paid daily at 01:30 UTC.

**FRR Delta definition** (secondary source, from Bitfinex Help Center summary):
> FRR Delta is the difference between FRR and a user's selected fixed-rate offer, determining whether a funding offer is competitive in the market.

**Derived hypothesis weight**:
- H1 (per-day): **supports** — interest rates explicitly quoted as "daily" percentages (0.06% daily = ~21.9% annualized)
- H2 (per-second): **refutes** — no per-second language; updates hourly, not per-second
- H3 (per-period × avg_period): **silent** — Help Center does not discuss avg_period in FRR context
- H4 (per-period-second): **refutes** — no period-second language found
- H5 (annualized / 365): **silent** — Help Center states daily rate; annualization formula (daily × 365 or daily / 365 to get spot rate) not explicitly explained in fragments recovered

## 3. Stage 2 empirical results

**Sample**: 147528 rows, fUSD + fUST, year range 2016-2026.
**Generated**: 2026-05-10T14:40:06.495002+00:00.

### 3.1 Hypothesis comparison

| ID | Predictor | R² | Per-year slope CV | Per-symbol slope diff | Median rel err | Pass |
|---|---|---|---|---|---|---|
| H1 | frr | 0.2168 | 0.5765 | 0.6366 | 0.3712 | ✗ |
| H2 | frr × 86400 | 0.2168 | 0.5765 | 0.6366 | 0.3712 | ✗ |
| H3 | frr × avg_period | 0.0041 | 1.1084 | 1.2284 | 0.5925 | ✗ |
| H4 | frr × avg_period × 86400 | 0.0041 | 1.1084 | 1.2284 | 0.5925 | ✗ |
| H5 | frr / 365 | 0.2168 | 0.5765 | 0.6366 | 0.3712 | ✗ |

### 3.2 Verdict: Gate 1 FAIL

No hypothesis met all 4 thresholds (R²>0.99, slope_cv<5%, slope_diff<5%,
median_rel_err<5%).

**Best candidate**: H1 with R²=0.2168 (failed on R²<0.99, slope_cv=0.5765≥0.05,
slope_diff=0.6366≥0.05, median_rel_err=0.3712≥0.05 — all 4 thresholds).

**Implication**: FRR is not a simple unit conversion of candle.close.
Phase 3b proceeds with 4🟢 strategies only; 2🟡 (FRR-trend, SpikeDetect)
推遲到 Phase 3c with extended hypothesis set (e.g. multivariate with
funding_amount ratio).

## 4. Conversion factor with provenance

**Status**: not derived (Gate 1 FAIL). conversion code 不 commit；
`market_rate_source="frr"` 引擎仍視為未通電。

## 5. Caveats

- Stage 1 reference 三 source 結果（PASS/silent/FAIL）：see sections 2.1-2.3
- Stage 2 sample 限 fUSD + fUST × 1h × p2；其他 timeframe / period_agg 未驗證
- 若有第三個 stablecoin（fEURT 等）上線，invariance 需重驗
- Conversion factor 的精度由 Stage 2 OLS 決定；Decimal 化時用 str(float) 保留 ~15 位有效數字
- FRR 的 amount-weighted average 定義（per Bitfinex Help Center）暗示樣本權重隨 funding amount 變化；invariance gate 對此不直接修正，靠長時間樣本攤銷

## 6. Next: Phase 3b

- Phase 3b 只跑 4🟢（24 runs，candle_close proxy）
- 2🟡 推遲 Phase 3c：擴展 hypothesis 集（multivariate w/ funding_amount_used /
  funding_amount，考慮 lower-50% lifetime weighting）
