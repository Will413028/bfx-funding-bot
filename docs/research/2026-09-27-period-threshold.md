# Period threshold: 2d by default, longer only above a rate threshold?

**Date**: 2026-09-27 · **Status**: research only. No live config, envelope or policy was changed (the live envelope still allows period 2–2).
**Question** (owner): should the bot stop always offering 2 days and instead lend 7 days (14/30 evaluated for completeness) only when the rate is above a threshold? If so, at what threshold?

## Recommendation

**Keep always-2d. Do not add a threshold rule now.** No threshold makes a 7-day rule clearly worth adopting:

1. **7d has no measured price history.** There is no p7/p14 candle series. The book has recorded exact-7d asks only since 2026-07-19, and they sit a median **+3% to +6%** above the best 2d ask (measured). A synthetic backtest over 2018-12 to 2026-07 prices 7d at 2d +4.8% (estimate). Its best rule, *7d when r7 ≥ 0.0002/day (7.3% APR) and r7 ≥ r2*, adds **+0.7 to +1.1 pp** median-month net APR when borrowers repay independently of price. Under rate-driven refinancing it adds **−0.06 to +0.03 pp**. On the 70-day book window it adds **+0.1 to +0.4 pp** (measured prices, 3 months). At the current size (~300 USDT) that is at most about $3 a year.
2. **Early repayment decides the question, and live evidence points the wrong way.** When returns are uniform (held 100%, 50% or 25% of the term), every long-tenor gain survives. When the borrower returns the credit as soon as the 2d market is ≥20% cheaper, the rational thing for a borrower with a free early return, every gain falls to about 0 (−0.6 to +0.5 pp across all rules and tenors). The only live data point fits that behaviour. The two 0.00019999 credits (466642176/177) were returned 14 minutes after creation on 2026-09-25, a day when p2 printed intra-hour lows of 0.000076–0.00016 (n = 2, anecdotal).
3. **The one large measured effect is a 30d level premium, not a threshold effect, and it cannot be executed now.** Traded p30 rates ran a median **+16% to +39%** above p2 in 2024–2026. Lending 30d whenever r30 ≥ r2 beats always-2d by **+1.7 to +2.6 pp** median-month (fUST) in the uniform-hold scenarios. Raising the threshold does not improve the median; from 0.0004/day upward it drops to 0. Refinancing cuts the gain to +0.1 to +0.2 pp. A visible exact-30d ask exists in only **5–10% of hours** (measured, since 2026-07-19), and the p30 series has not been recorded since 2026-07-19.

**If the owner wants a live trial anyway**, the candidate with the best worst-case is: *offer 7d when the best visible exact-7d ask is ≥ max(0.0002/day, current 2d rate); otherwise 2d*. The trailing-30d p80-of-r2 variant performs about the same. Treat it as a measurement experiment, not a yield change.

**What would change this**

- Live long-credit hold durations show that returns do not follow rate drops (median hold ≥ 50% of term, and holds not shorter after the 2d market falls). The uniform-hold rows would then apply: about +1 pp for 7d at ≥ 0.0002 and about +2 pp for 30d, which becomes worth doing.
- A recorded p7 (and p14) trade series shows a 7d **traded** premium of roughly ≥ 10% over p2. Bitfinex publishes `trade:1h:fUST:p7` candles, but backfilling them is an external download and needs the owner's approval.
- 30d exact-ask visibility rises well above 10% of hours, and the p30 recording resumes, so the 30d premium can be re-measured after 2026-07.
- Deployed capital grows enough for 1–2 pp to matter.

## Data and window

| source | table / series | window | notes |
|---|---|---|---|
| 2d rate | `funding_candles` fUST `p2` 1h (close, high) | 2018-12-22 .. 2026-07-19 (full); 2024-01-01 .. 2026-07-19 (recent) | also fUSD 2016-08-01 .. 2026-07-19 |
| 30d rate | `funding_candles` fUST/fUSD `p30` 1h | same | **p30 stops at 2026-07-19** (fUST 02:00, fUSD 04:00); only hours with a 30d trade have a row |
| 7d / 14d rate | none in candles (only `p2`, `p30`, `a30` exist) | — | 7d is SYNTHETIC in the candle runs; 7/14 measured only from the book |
| book | `funding_book_snapshots` fUST (top-25 P0 levels; asks `[rate, period, count, amount]`) | 2026-07-19 14:16 .. 2026-09-27 05:18 UTC, 2,100 snapshots | irregular cadence: about 1/h until 2026-09-23, about 12/h from 2026-09-24 |
| fee | Bitfinex 15% of interest (`backtest.config.fee_rate`) | — | all APRs are net |

Production Postgres was reached read-only through the SSH tunnel on `127.0.0.1:5433`, using SELECT only.

Reproduce (from `backend/`; `.env` → `DATABASE_URL`):

```sh
# visibility (existing Gate-A0 tool); since-ms = now - 7d at run time (1789881649464 here)
uv run python -m scripts.audit_book_period_coverage --symbols fUST --periods 2,7,14,30 --since-ms 1789881649464
# threshold study (new tool; pure core in modules/backtest/period_threshold.py)
uv run python -m scripts.run_period_threshold --symbol fUST --start 2018-12-22 --end 2026-07-19
uv run python -m scripts.run_period_threshold --symbol fUST --start 2018-12-22 --end 2026-07-19 --rate-cap 0.002
uv run python -m scripts.run_period_threshold --symbol fUST --start 2024-01-01 --end 2026-07-19 --premium-bucket month
uv run python -m scripts.run_period_threshold --symbol fUSD --start 2016-08-01 --end 2026-07-19 --rate-cap 0.002
uv run python -m scripts.run_period_threshold --symbol fUST --source book --start 2026-07-19 --end 2026-09-28
```

Visibility by day and by month (SQL):

```sql
with s as (
  select captured_at_ms,
    exists(select 1 from jsonb_array_elements(payload->'asks') a where (a->>1)::int=2)  p2,
    exists(select 1 from jsonb_array_elements(payload->'asks') a where (a->>1)::int=7)  p7,
    exists(select 1 from jsonb_array_elements(payload->'asks') a where (a->>1)::int=14) p14,
    exists(select 1 from jsonb_array_elements(payload->'asks') a where (a->>1)::int=30) p30
  from funding_book_snapshots where symbol='fUST')
select to_char(to_timestamp(captured_at_ms/1000) at time zone 'UTC','YYYY-MM-DD') d, count(*),
  round(avg(p2::int)*100,1), round(avg(p7::int)*100,1), round(avg(p14::int)*100,1), round(avg(p30::int)*100,1)
from s group by 1 order by 1;
```

## Model

The engine (`engine.py` ~L290) keeps every credit for its full period, so it cannot answer this question. The new simulator (`modules/backtest/period_threshold.py`) works on an hourly grid and holds one unit of capital with simple interest:

- **Rule**: lend the long tenor L when a long price reference exists, `rL ≥ r2`, and either `rL ≥ threshold` (absolute daily rate) or `r2 ≥` its trailing 30-day percentile (no lookahead). Otherwise lend 2d. `always_2d` is the baseline.
- **Executable price (candle runs)**: an offer posted in hour i is priced at hour i−1's close and fills only if that tenor printed at or above it during hour i (`high[i] ≥ close[i−1]`). This removes same-hour lookahead and stops a one-off spike print from being "captured". Without it, p30 closes of 0.07/day (the rate cap) produced +30 pp artefacts. A `--rate-cap 0.002` run is the no-spike view. Only 25% of fUST hours (16,776 / 66,384) have an executable 30d price.
- **Early-repayment scenarios**, applied to 2d and long credits alike; after any return the capital re-enters the rule at the then-current rates after a 1 h re-lend delay:
  - `held 100%` / `held 50%` / `held 25%`: every credit is returned after that share of its term, independently of price.
  - `refinance if r2 < 0.8× locked`: the adverse case. The borrower keeps the credit while it is cheap and returns it the first hour the 2d market is ≥ 20% below the locked rate. This is the borrower's free option.
- **Metrics**: full-window Δ net APR vs always-2d, and the **median per-month Δ** with the share of months won. Each month is simulated independently, so fat-tail months cannot drive the result. Full-window Δ values much larger than the median Δ come from spike months.

## Book visibility per period (measured)

**Last 7 days** (fUST, since 2026-09-20 05:20 UTC; 831 snapshots, 88% of them captured from 2026-09-24 on because the cadence rose):

| period | exact-period ask visible | ≥ period visible | median depth (USDT) | median best ask | median premium over 2d best ask |
|---|---|---|---|---|---|
| 2 | 100.0% | 100.0% | 170,373 | 0.00017156 | — |
| 7 | 68.7% | 78.5% | 1,161 | 0.00018685 | +4.8% |
| 14 | 2.8% | 21.1% | 361 | 0.00020548 | +13.8% |
| 30 | 8.9% | 18.8% | 5,000 | 0.00020000 | +11.1% |

**Hour-weighted, whole book window** (last snapshot per hour, 1,704 hours):

| month | 7d visible | 7d premium | 14d visible | 14d premium | 30d visible | 30d premium | median best 2d ask (APR) |
|---|---|---|---|---|---|---|---|
| 2026-07 (from 07-19) | 61% | +3.1% | 10% | +13.1% | 9% | +18.4% | 7.3% |
| 2026-08 | 57% | +4.7% | 8% | +5.0% | 5% | +11.8% | 5.5% |
| 2026-09 | 42% | +6.1% | 5% | +9.4% | 10% | +15.9% | 5.9% |

**The external review's numbers (last 7 days, 632 snapshots: 2d 100%, 7d 69.5%, 14d 1.9%, 30d 3.6%) mostly hold.** 2d and 7d reproduce closely (100% / 68.7%). 14d and 30d are small in both counts, but they depend heavily on the window. Daily 30d visibility swings between 0% and 48.5%, so 3.6% versus 8.9% reflects when each count was taken, not a disagreement. The review's count of 632 is lower than the 831 here because the 7-day window moved while snapshots arrive about 12 per hour. Snapshot-weighted shares over-weight the days since 2026-09-24. Use hour-weighted shares, and over longer windows, for decisions.

Caveat on "visible": snapshots hold only the **top 25** ask levels, which is the same window live eligibility checks. That window is filled almost entirely by 2d asks, so a 14d or 30d ask priced above the 25th 2d level is out of view, not necessarily absent. Under exact-period pricing that still means there is **no price reference** in about 90–95% of hours for 14d/30d and about 40–55% for 7d.

## Rate premium of longer periods vs 2d over time (measured, traded rates)

Median over same hours of p30 close / p2 close − 1. "30d ref available" is the share of hours with any 30d trade.

| year | fUST 30d ref available | fUST premium | fUSD premium |
|---|---|---|---|
| 2019 | 48% | +1.5% | +0.0% |
| 2020 | 60% | +1.4% | +0.2% |
| 2021 | 44% | +5.3% | +26.0% |
| 2022 | 20% | +9.4% | +92.2% |
| 2023 | 47% | +7.9% | +103.7% |
| 2024 | 55% | +39.0% | +58.9% |
| 2025 | 63% | +35.2% | +50.1% |
| 2026 (to 07-19) | 55% | +16.2% | +54.9% |

The fUST monthly premium over 2024-01 .. 2026-07 ranges from −0.9% (2026-04) to +119.8% (2024-05) and is usually +20% to +50%. It collapses in rate-rich months (2026-03 +2.1%, 2026-07 +1.2%). **The 30d premium is largest when rates are low**, which works against an "only go long when the rate is high" rule. The traded 30d premium is also much larger than the visible 30d ask premium (+12% to +18%), which suggests thin, lumpy 30d prints. The median 30d hourly volume in 2024+ was 12k USDT, against 985k for 2d. No 7d or 14d traded series exists.

## Candidate thresholds: Δ net APR vs always-2d

Cells show **median per-month Δ (pp) (share of months won)**, and in brackets the full-window Δ, which includes the fat tail. `long%` is the share of hours in a long credit under held 100%.

### 7d SYNTHETIC: fUST 2018-12 .. 2026-07, r7 = r2 × 1.048, fillable whenever 2d is (ESTIMATE)

Always-2d net APR: 5.92% / 5.86% / 5.67% / 4.14% under held 100% / 50% / 25% / refinance.

| rule | long% | held 100% | held 50% | held 25% | refinance |
|---|---|---|---|---|---|
| 7d whenever r7 ≥ r2 | 99% | +0.41 (67%) [+0.68] | +0.28 (64%) [+0.45] | +0.58 (84%) [+0.63] | **−0.30 (17%)** [−0.51] |
| **7d if r7 ≥ 0.0002 (7.3% APR)** | 58% | **+1.07 (80%)** [+1.34] | +0.73 (76%) [+1.25] | +0.95 (86%) [+1.01] | +0.01 (70%) [−0.03] |
| 7d if r7 ≥ 0.0003 (10.9%) | 30% | +0.19 (54%) [+1.58] | +0.43 (64%) [+1.21] | +0.63 (73%) [+0.83] | 0.00 [−0.01] |
| 7d if r7 ≥ 0.0004 (14.6%) | 14% | 0.00 (33%) [+0.90] | 0.00 (43%) [+1.05] | +0.16 (59%) [+0.62] | 0.00 |
| 7d if r7 ≥ 0.0005 (18.2%) | 8% | 0.00 (24%) [+0.93] | 0.00 [+0.66] | 0.00 [+0.62] | 0.00 |
| 7d if r7 ≥ 0.0010 (36.5%) | 2% | 0.00 (8%) [+0.92] | 0.00 [+0.43] | 0.00 [+0.25] | 0.00 |
| 7d if r2 ≥ trailing-30d p80 | 35% | +0.51 (72%) [+0.91] | +0.59 (82%) [+1.39] | +0.61 (85%) [+0.77] | +0.01 (66%) [+0.01] |
| 7d if r2 ≥ trailing-30d p90 | 22% | +0.25 (60%) [+0.94] | +0.39 (76%) [+0.97] | +0.50 (79%) [+0.70] | 0.00 |

- With a 0% premium (r7 = r2), the ≥ 0.0002 rule still gives +0.74 / +0.52 / +0.54 median under the uniform scenarios and 0.00 (−0.06 full-window) under refinance. The gain comes from **timing**: holding a high rate while 2d mean-reverts.
- Recent window (2024-01 .. 2026-07, +4.8%): ≥ 0.0002 gives +0.98 (87%) / +0.76 (94%) / +0.96 (100%) / +0.01 (81%).

### 7d MEASURED from the book: fUST 2026-07-19 .. 2026-09-27 (3 months, best visible exact-7d ask)

Always-2d net APR (best 2d ask): 4.23% / 4.18% / 3.96% / 3.67%.

| rule | long% | held 100% | held 50% | held 25% | refinance |
|---|---|---|---|---|---|
| 7d whenever r7 ≥ r2 | 78% | +0.63 [+0.39] | +0.52 [+0.55] | +0.45 [+0.40] | −0.08 [−0.04] |
| 7d if r7 ≥ 0.0002 | 16% | +0.13 [+0.13] | +0.21 [+0.19] | +0.44 [+0.40] | 0.00 [−0.02] |
| 7d if r7 ≥ 0.0003 and above | 0% | never triggered | | | |

This window never reached 0.0003 on a visible 7d ask. With 3 months, the win shares carry no information.

### 30d MEASURED (p30 trades, executable-price model)

fUST 2018-12 .. 2026-07 (always-2d 5.92 / 5.86 / 5.67 / 4.14%):

| rule | long% | held 100% | held 50% | held 25% | refinance |
|---|---|---|---|---|---|
| 30d whenever r30 ≥ r2 | 66% | +1.68 (73%) [+2.41] | +1.69 (83%) [+3.31] | +2.24 (91%) [+7.88] | +0.09 (76%) [+0.09] |
| 30d if r30 ≥ 0.0002 | 56% | +1.91 (74%) [+6.78] | +2.03 (77%) [+3.37] | +2.38 (87%) [+14.61] | +0.07 (71%) [+0.15] |
| 30d if r30 ≥ 0.0003 | 51% | +1.93 (66%) [+8.94] | +2.03 (72%) [+3.16] | +2.28 (76%) [+20.19] | +0.03 (59%) [+0.13] |
| 30d if r30 ≥ 0.0004 | 28% | 0.00 (41%) [+7.69] | +1.34 (54%) [+8.48] | +1.79 (63%) [+14.85] | 0.00 [+0.11] |
| 30d if r30 ≥ 0.0005+ | ≤ 18% | 0.00 | 0.00 | 0.00 | 0.00 |
| 30d if r2 ≥ trailing p80 | 31% | 0.00 (51%) [+1.50] | +0.78 (65%) [+2.77] | +1.13 (75%) [+13.55] | 0.00 [−0.05] |

- The full-window values in brackets are spike-driven. With `--rate-cap 0.002` the held-25% column at ≥ 0.0002 falls from +14.61 to +3.01, while the medians do not change.
- fUST 2024-01 .. 2026-07: ≥ 0.0002 gives +2.58 (94%) / +2.20 (87%) / +2.68 (100%) / **+0.15 (90%)**.
- fUSD 2016-08 .. 2026-07 (capped): ≥ 0.0002 gives +2.27 / +2.68 / +2.58 / **+0.29**; ≥ 0.0003 gives +2.66 / +3.43 / +2.71 / +0.17.

### 14d MEASURED from the book

A 14d exact ask was visible in 5–10% of hours, so rules were long only 0–20% of the time. Every median-month Δ is 0.00 to +0.14 pp. The data cannot price a 14d threshold.

## What the early-repayment sensitivity did

- **Uniform early repayment (held 50% / 25%)** does not overturn anything. The long tenor's benefit is earned per hour held, and returned capital re-lends at 2d, so medians barely move and sometimes rise. The full-hold engine assumption is therefore *not* what inflates these results.
- **Rate-contingent repayment (refinance)** removes the benefit in every table: medians are −0.41 to +0.32 pp, and the no-threshold 7d rule is negative (−0.30, winning only 17% of months). A threshold of ≥ 0.0002 protects the downside (about 0), but the upside is gone too. It also cuts **always-2d** itself from 5.92% to 4.14%, so refinancing risk exists regardless of tenor.
- **What survives both**: a rule with a threshold (≥ 0.0002/day, or r2 ≥ trailing p80) is not worse than always-2d in any scenario (worst median −0.00 to +0.01 pp). A tenor switch *without* a threshold can lose. Whether it gains anything depends entirely on unmeasured borrower behaviour.

## Caveats (fill probability and visibility)

- **Estimate vs measurement.** Measured: book visibility, book 7/14/30 ask premia, p30/p2 traded premia, and the 30d and book-window backtests given the fill model. Estimated: every 7d result on candles (synthetic price and availability), and all early-repayment scenarios (assumed behaviour; the live sample is n = 2).
- **Fill.** Candle runs assume our offer fills whenever the tenor printed at or above the previous hour's close. There is no queue position and no size limit. That is optimistic for 30d (thin: 12k USDT per hour median) and for synthetic 7d (assumed fillable whenever 2d is, while 7d asks are visible only 42–69% of the time). Book runs assume a fill at the best visible ask, which is a join-the-queue price, not a fill.
- **Exact-period pricing.** Live eligibility needs an exact-period ask in the top-25 book. 14d and 30d fail that in about 90–95% of hours, so a live rule would mostly fall back to 2d no matter the threshold.
- **Windows.** p30 and fUSD candles stop at 2026-07-19 / 2026-09-09, and the book covers 70 days of one regime (median 2d about 5.5–7.3% APR), which never reached 0.0003 on a 7d ask.
- **Simplifications.** Simple interest on a single capital unit, a 1 h re-lend delay (it penalises 2d slightly more than long tenors), and a 20% refinance margin chosen by judgement.

## Code added (research tooling)

- `backend/src/bfx_funding_bot/modules/backtest/period_threshold.py`: pure simulator (`simulate`, `RepaymentScenario`, `ThresholdRule`, `limit_fill_grid`, `trailing_percentile`, `monthly_deltas`, `premium_by_bucket`).
- `backend/scripts/run_period_threshold.py`: read-only DB runner (candles or book source).
- `backend/tests/modules/backtest/test_period_threshold.py`: 12 unit tests.
