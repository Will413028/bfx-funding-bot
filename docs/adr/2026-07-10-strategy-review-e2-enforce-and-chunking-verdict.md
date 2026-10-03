---
title: 策略 review — E2 提前 enforce + 切單裁決（不做 150-chunking、spike-rung 只 observe）+ benchmark utilization 修正
date: 2026-07-10
status: active
tags: [bfx-funding-bot, decision, strategy, execution-layer, pricing, benchmark, bitfinex]
related-commits:
  - "b2ead78^..1038cee"
---

# 策略 review — E2 提前 enforce + 切單裁決 + benchmark utilization 修正

## Context

operator 問兩個問題：「為何 bot 利率都沒比 FRR 高？」「為何一大筆放貸、不像其他 bot 切 150 小單？」22-agent review（4 readers × 3 analysts × 15 adversarial verifiers，13/15 建議存活）回答並當日全落地。Prior state：E1 已 enable（07-07）、E2 shipped observe-only 等 ~07-14 date gate（[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md)）；首週 AlwaysFRR > Bot（live 實測低於 FRR baseline）。機制結論：`rate=close` + clamp ≤ ask 使成交價上限 = 市場邊際價，而 FRR 是 volume-weighted 每小時平均（spike 時段權重大）→ 結構性追不上；且 AlwaysFRR benchmark 假設 full utilization、本身灌水。

## Options Considered

**切單**：
- **A. 維持 lump-per-cell（選用）**：Bitfinex 原生 partial fill，一張大單撮合上等價於 N 張同價小單
- **B. same-rate 150-chunking**：cap/153 ≈ 數十筆 submit（自家 limiter 30/min → >2min churn）+ 數十倍 event/claims 膨脹 + sweep（3 cancels/tick）約半小時以上清理
- **C. spike-rung ladder（觀察後再說）**：每筆 fill 切 10-15% 掛 ask×k — 競品（MikaLendingBot/eAndrius/instabot42）切單的真實理由 = 靜態 ladder 替代動態改價；E1+E2 已是動態版，ladder 唯一增量 = spike capture
- **D. FRRDELTAVAR sleeve（30-50% cap）**：直接買 benchmark

**E2 flip 時機**：date gate（~07-14）vs **log gate 現在翻（選用）**。
**Benchmark utilization**：traded≥FRR 時段比例 proxy vs **市場級 `funding_amount_used/funding_amount`（選用）**。

## Decision

- **D1**：E2 提前 enforce（07-10 01:28 UTC），gate 從「時間」換成「證據」（E1 72h：1 cancel/0 error；clamp observe：2 undercut/2 floor 分布健康）。flags source of truth 移入 committed `deploy/vm/canary.env`。
- **D2**：切單選 A；B 明確否決；C 只上 observe-only（`BFX_LADDER_OBSERVE` 預設 off、enforce path 刻意不建）；D 被 verifier 依前 ADR D2 否決（重申：等 ≥8 windows，先跑 backtest 變體 MR-with-FRR-floor）。
- **D3**：量測雙修 — AlwaysFRR 加 utilization-adjusted 欄（idealized 保留當 upper bound，cap-gate spread 改讀新欄）+ `config_regime` 表/fill-latency report（E2 效果幾天內可歸因，不必等 8 weekly windows）。
- **附帶**：單 active cell 擱淺修（`cap_per_cell = max(70%×cap, cap/n_active)`，兩處同步）。

## Rationale

- **D1**：date gate 的本意是「確認 E1 穩定」，72h log 證據已充分，多等 4 天純 idle cost；而非固守日期。代價：樣本僅 4 筆 clamp observe events（資金近滿借出、submits 稀少），靠 D3 的快速歸因兜底。
- **D2**：B 相較 A 零撮合收益、純 overhead（venue partial fill 已提供小單的全部好處）；C 不直接 enforce 是因與 E1 sweep（30min 後砍 >ref×1.10）和 E2 UNDERCUT（壓回 ask−1tick）**互相打架**，需豁免 tag 先有 observe 數據證明 uplift 才值得建；不選 D：污染 E3 正在量測的 E1/E2 A/B、且「benchmark 可達成」未經證實（fill 無保證）。
- **D3**：不用 traded≥FRR proxy — 它混淆成交頻率與 capital-time utilization（FRR 成交鎖倉 ≥2d，稀疏 crossing 仍可近滿倉）會系統性低估 → spread 假轉正 → cap-gate 誤放行；市場級 used/amount 直接可觀測。代價：仍是市場級近似（非自身 fill 模擬）。

## Result

- `git log --oneline b2ead78^..1038cee`（10 commits；SDD 6 tasks 每 task 獨立 review、2 個 opus fail-first、final whole-branch READY_TO_MERGE）+ deploy 修 `2748514`。
- VM 驗證：`config_regime_recorded clamp=True reprice=True`、reconcile 乾淨（realized 不變）、report 對真資料 smoke（E1-only baseline：4 claims/75% fill/p50 1.1m/p90 8.0m）。
- 部署揭露真 bug：`deploy-vm.sh` 在 pull 前組 `.env.runtime` → E1 被靜默回退 ~15min（idle 零影響）→ 修 pull-first。

## Followup

- E2 效果觀察（~07-13 起）：`report_execution_quality` 比對 regime 桶；fill% 掉 → canary.env 翻回 false 再 deploy。
- 下 session 三件事：bot.env 刪 stale `BFX_SERVICE_VERSION`、查 auth WS reconnect flapping、T3 utilization `min(1, ratio)` clamp + minor roll-up（清單在 repo `.superpowers/sdd/progress.md`）。
- FE attribution 頁加第 4 條線（`baselineFrrUtilAprNetPct` API 已有）。
- spike-rung observe 實驗要開時：`BFX_LADDER_OBSERVE=true`（先確認 E2 已穩定），觀 `ladder_would_post`。

## Lessons

### Rules

- **R1**：`Rule: date gate 只是 proxy — gate 的本體是證據；證據提早齊了就翻，證據不齊日期到了也不翻。`
- **R2**：「別的 bot 都這樣做」要先問它在替代什麼。`Rule: 競品行為（切單）是其架構限制（fire-and-forget）的補償時，抄過來前先確認自己是否已有更強的等價物（動態 reprice/clamp），只補真正的增量（spike capture）。`

### Observations

- **O1**：對抗性驗證有效抓自家過度興奮 — 兩條 FRRDELTAVAR 建議被 verifier 以既有 ADR + 一週樣本不足否決，避免了污染 E3 A/B 的真錢動作。
- **O2**：utilization 折算的異常方向分析（>1 會抬高 benchmark → cap-gate 更保守 = fail-conservative）是決定「clamp 可延後」的關鍵——先分析失效方向，再決定 hardening 的優先級。

## Related

- 實作 plan：`2026-07-10-strategy-review-batch.md`（commit `1038cee`；原文已不在 repo，本 ADR 即紀錄）
- [2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md) — D 否決依據（FRR parking D2）、E1→E2→E3 主軸
- [2026-07-06-e2-book-aware-clamp](2026-07-06-e2-book-aware-clamp.md) / [2026-07-07-e3-measurement-and-diagnostics-realm-authority](2026-07-07-e3-measurement-and-diagnostics-realm-authority.md) — 被本次翻牌/擴充的兩軌
- [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) — FRR 非市場利率的更早結論（本次補上 volume-weighting 機制解釋）
