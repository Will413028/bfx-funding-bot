# Session Handoff — 2026-05-10 Phase 3a

> 給下次 session 1 分鐘接手用。

## 上次 session 結尾狀態

**Phase 3a (FRR Unit Investigation) — Gate 1 FAIL 路徑完整 ship。** 6 commits in `bfx-funding-bot` repo，working tree clean。

```
c10f74f 🔧 Chore: Phase 3a Gate 1 FAIL — check_frr_unit → raw r² diagnostic
a7dee8a 📝 Docs: Phase 3a Stage 2 — empirical hypothesis verdict
292b20a ✨ Feat: scripts/investigate_frr_unit.py — 5-hypothesis OLS regression
2453b74 🔧 Chore: add numpy/pandas dev deps for FRR investigation script
ca9396c 📝 Docs: Phase 3a Stage 1 reference findings — bitfinex-api-py / ccxt / Help Center
331c28c 📝 Docs: scaffold Phase 3a research note + data/research dir
18499a5 📝 Docs: v2 Phase 3a implementation plan        ← 計畫
2bb9938 📝 Docs: v2 Phase 3a FRR unit investigation design   ← spec
```

## 立刻要做（先做這個！）

`~/second-brain/wiki/projects/bfx-funding-bot.md` 有 Phase 3a Lessons Learned 一行**未 commit**。second-brain 一天一 commit 規則：

```bash
cd ~/second-brain && claude
> 收工      # 觸發 /daily-wrap-up
```

訊息格式：`daily: 2026-05-10 v2 Phase 3a FRR unit unresolved (Gate 1 FAIL) + ship raw r² diagnostic`

## Gate 1 FAIL 的關鍵發現（spec 已預備此路徑，不是 surprise）

5 hypothesis 全 missed thresholds（147,528 sample，fUSD+fUST 2016-2026）：

| H | R² | slope_cv | slope_diff | median_rel_err | pass |
|---|---|---|---|---|---|
| H1 (frr → daily) | 0.2168 | 0.5765 | 0.6366 | 0.3712 | ✗ |
| H2 (frr × 86400) | 0.2168 | 0.5765 | 0.6366 | 0.3712 | ✗ |
| H3 (frr × avg_period) | 0.0041 | 1.1084 | 1.2284 | 0.5925 | ✗ |
| H4 (frr × avg_period × 86400) | 0.0041 | 1.1084 | 1.2284 | 0.5925 | ✗ |
| H5 (frr / 365) | 0.2168 | 0.5765 | 0.6366 | 0.3712 | ✗ |

**3 個非顯而易見洞察**：

1. **H1/H2/H5 R² 一樣**：純 frr scaling 在 OLS w/ intercept 下 mathematically indistinguishable — slope 自吸常數。R²=0.22 太低代表 FRR 不是 frr 的 linear function 任何時間單位都不行。
2. **H3/H4 比 H1 更糟**（R²=0.004）：`avg_period` 是反訊號，不是 normalization factor。
3. **Per-year slope swing 9×**：2016 ratio≈387 → 2023 ratio≈42。**Bitfinex 改過 FRR 計算方法**，用單一常數 conversion 在跨年 sample 上不可能 work。fUSD vs fUST slope_diff=0.64 進一步證實。

## 下次 session 該做的（priority order）

### 1. Phase 3b brainstorm（建議下次 session 開的）

**Scope 縮小**：因為 Gate 1 FAIL，2🟡 (FRR-trend / SpikeDetect) 推遲到 Phase 3c，所以 Phase 3b 只跑 4🟢：
- RatePercentile (P25/P50/P75 thresholds)
- DynamicPeriod (period_days range, regime-aware)
- WeekendPremium (Fri/Sat/Sun 加碼曲線)
- MeanReversion (EMA window)

矩陣：4 strategies × 2 symbols × 3 period_agg = **24 runs**（不是原本 36 runs）。

`market_rate_source="frr"` 引擎仍未通電，繼續用 `candle_close` proxy。

走 brainstorming → writing-plans → subagent-driven 同樣流程。Brainstorm 時要決定的：
- 各策略 parameter space 範圍
- 排名 metric（建議 net_monthly_return / max_DD / fill_rate 三軸非單一指標）
- 統計顯著性（24 runs 偏少，t-test 怕 underpowered，考慮 bootstrap）
- Walk-forward vs single-pass（Phase 1 spec 寫過 walk-forward 推遲 v3+，但 4 個策略開始可以 retire 此 punt）

### 2. （可選）Phase 3a `/recording` — 把方法論教訓寫進 second-brain

3 個值得進 `wiki/tech/` 的 lesson（跨專案會再撞）：
- **「未知 API field 是什麼單位」problem framework**：reference-first + empirical confirmation > empirical-first
- **OLS w/ intercept 的 unit-conversion hypothesis test 陷阱**：純 scaling hypothesis mathematically indistinguishable，必須加 invariance gate
- **Per-year slope drift 是 silent killer**：raw CV<5% 抽單時間點會誤判 PASS，必須 split by year/regime

放 `wiki/tech/api-unit-investigation.md` 或併進 `wiki/tech/quantitative-finance.md`（如果之後常用）。

### 3. Phase 3c —不急

3c 啟動條件 = Phase 3b 跑完看 4🟢 結果，如果策略表現不夠好需要 2🟡 補強，才回頭解 FRR。Hypothesis 集擴展方向：
- H6: multivariate `close ~ a × frr + b × frr × (funding_amount_used / funding_amount) + c`
- H7: regime split by year（pre-2020 vs post-2020 各自 fit），看是否兩段都 pass invariance gate
- H8: 對照 ccxt LAST_APR field（`/v2/status/deriv` endpoint，跟 funding_stats 不同 endpoint），看是否 FRR + LAST_APR 共同 reconstruct

## 重要 context（容易踩坑）

- **gh account trap**: 個人 startup repo 在 `Will413028`，但機器 active 帳號預設是 `will-dailyfresh`。Push / `gh pr create` / `gh repo view` 前必先 `gh auth switch -u Will413028`。
- **backend_py cwd rule**: 所有 `pytest / mypy / ruff / alembic / uv run` 必須 `cd backend_py` 才會走 uv 管的 Python 3.13。從 repo root 直接跑會撞 pyenv 3.12 sqlalchemy import error。
- **alembic check 壞掉**: pre-existing `psycopg2` import error（asyncpg 切換時 alembic 配置沒同步遷移）。Phase 3a 沒 touch ORM tables 所以無 schema drift 風險，但**這是個埋藏 bug** — 需要修才能放心 alembic autogenerate 新 migration。下次摸到 schema 時要先解。
- **bash unicode trap**: ruff 對 `×`（U+00D7 multiplication sign）/ `–`（en dash）/ `–` 等 ambiguous unicode 在字串裡會跳 RUF002 warning。寫 spec 時用沒差，寫 code label 字串時改 ASCII 比較順。
- **`data/research/*.json`** gitignored — re-derivable via `cd backend_py && uv run python scripts/investigate_frr_unit.py`。下次想看 Stage 2 結果直接重跑（~5s on Neon）。
- **second-brain CLAUDE.local.md 預警未過時**: 12 deferred sub-decisions（worker model / real-money safety / observability）仍 pending，維持 v2 全 phase 完才解凍 timeline，不要 Phase 3b 時順手碰。

## 環境快照

- 當前 branch: `main`（bfx-funding-bot 直 commit 到 main per CLAUDE.md daily commit rule）
- Working tree: clean (bfx-funding-bot)
- second-brain working tree: `wiki/projects/bfx-funding-bot.md` modified (uncommitted)
- DB rows in Neon: 547,278（Phase 2 backfill 結果，Phase 3a 沒新增 row）
- pytest: 81 passed (was 84 pre-Phase 3a，-3 from removed `test_frr_unit_*`)
- mypy / ruff: clean
- numpy 2.4.4 / pandas 3.0.2 added as dev deps（Stage 2 script 用，production 不用）

## 入口檔案

- Spec: `docs/superpowers/specs/2026-05-10-phase3a-frr-unit-investigation-design.md`
- Plan: `docs/superpowers/plans/2026-05-10-phase3a-frr-unit-investigation.md`
- Research note: `docs/research/2026-05-10-frr-unit-investigation.md`
- Phase 2 result（含 3a appendix）: `docs/superpowers/specs/2026-05-10-phase2-result.md`
- 引擎升級 spec: `docs/superpowers/specs/2026-05-10-engine-upgrade-design.md`
- Stage 2 script: `backend_py/scripts/investigate_frr_unit.py`
- 新 diagnostic check: `backend_py/src/bfx_funding_bot/modules/backfill/checks.py:213` (`check_frr_unit_stability`)

## 一行起手式給下次 session

> Continue work on main. Last commit `c10f74f`. Phase 3a closed in Gate 1 FAIL path; 2🟡 deferred to 3c. Next: Phase 3b brainstorm — 4🟢 strategies × 2 symbols × 3 period_agg = 24-run matrix（candle_close proxy）。先 read `docs/superpowers/handoffs/2026-05-10-phase3a-handoff.md` 拿 context，然後走 brainstorming skill。second-brain wiki 那行 Lessons Learned 已在你機器 working tree 但未 commit，今天 daily-wrap-up 要記得收。
