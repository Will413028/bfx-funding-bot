---
title: Minimal canary — 真錢以「必要 guard 全開否則拒絕啟動」放行，G3 自動 halt 延後
date: 2026-05-25
status: active
tags: [bfx-funding-bot, decision, canary, safety, risk-management]
related-commits:
  - "37aa5b6^..86e8a15"
  - b17e0c4
---

# Minimal canary — fail-closed guard invariant，G3 自動 halt 延後

## Context

策略已過 WFO backtest + paper + shadow，venue reconcile 的 wire/auth 在 2026-05-25 驗過，PG SoT 已就位。第一筆真錢的剩餘阻擋：`load_config` 明確拒絕 `BFX_PHASE=canary`；而且每個 safety guard 都是 `if <cfg>.enabled` 才加入 chain（Phase 4.2 M2），設定檔可以把任何 guard 靜默關掉，`daemon.py` 對此只留了 TODO。prior state：phase 只是 lifecycle／observability 標籤，真正的真錢開關是 `BFX_EXECUTOR=bitfinex_live`（4.4a/b 已接線）；[2026-05-18-phase4-staged-rollout-paper-shadow-canary](2026-05-18-phase4-staged-rollout-paper-shadow-canary.md) 把 G3 定為 Phase 4 的**結束** gate，沒回答它是不是 canary 的**開始** gate。

**約束**：

- `external` 首個 canary 是 solo、小額（接近 venue 最低下單額）的真錢。
- `inherited` phase 與 executor 正交（`is_simulated` 跟 executor 走）；仍成立，本決策不新增 execution branch。
- `inherited` 既有 `SafetyConfig` validator 已保證「啟用的 loss limiter 必有 threshold」；仍成立，所以 invariant 只需檢查 `enabled`。

## Options Considered

- **基準. 真錢下單前的強制 pre-trade 風控 + 立即停機能力**：SEC Rule 15c3-5（Market Access Rule）要求預設的資金／信用門檻與可立即 disable 的控制，且不能由使用者自行繞過。
- **A. 先建 G3 P&L tracking-error 自動 halt + 真 `PnLLedger` 聚合，再上 canary**。
- **B. Minimal（選用）**：允許 `canary`，並在 `build_daemon` 加 fail-closed invariant——`phase == canary` 時 `manual_kill`、`auth_health`、`heartbeat`、`allocation_cap`、`realized_loss_24h`、`drawdown_from_peak` 六個 guard 任一未啟用就 `ValueError`、非零退出；`divergence_rate` 維持 optional。
- **C. 只允許 `canary`，guard 關閉時只記 warning**。

**invariant 放哪**：`build_daemon`（選用，phase 與 `safety_cfg` 同時在 scope）／`load_config`（只看得到 phase 字串）。

## Decision

- **D1**：選 B。真錢風控 MVP = 既有 loss/drawdown limiter + allocation cap + kill switch + 人工監看；G3 自動 halt、自動加碼、`divergence_rate` 必開、per-cell 重選全部延後。
- **D2**：invariant 是 `daemon.py` 的純函式，在 guard chain 建構前呼叫；paper／shadow 不受影響，仍可關 guard。
- **D3**：營運 profile 屬部署設定、不是程式碼（當時：cap 為小額 canary 上限、24h loss 門檻約 cap 的 1/10、drawdown ≈ 15%、只開 MeanReversion fUSD a30/p2）。

## Rationale

- **D1**：G3 自動 halt 是 scale-up 控制而不是上線門檻；在小額、樣本極少的 canary 上先建它是 premature validation（tracking error 需要數週資料才有意義）。**不選** C：warning 會被忽略，真錢帶著關掉的 guard 跑正是要封掉的 footgun。**相較**基準，B 就是它的最小實作——門檻強制、不可由設定繞過、kill switch 可立即停機。**代價**：canary 期間策略偏離 backtest 只能人工察覺。
- **D2**：`load_config` 看不到 `safety_cfg`，放那裡做不到；純函式讓每個 guard 個別關閉的情境都能單元測試。

## Result

- `git log --oneline 37aa5b6^..86e8a15`：spec、plan、`fd2f83e` 允許 canary、`86e8a15` invariant；2026-05-25 canary 上線（當時空跑、等待入金）。
- D3 profile 很快被取代：改 fUST-only、cap 提高為原 canary 的 3 倍（`8a7283f`），再由 [2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 的動態 CapitalPolicy 取代固定 cap。
- G3 於 2026-05-30 以週報而非自動 halt 落地（[2026-05-30-g3-live-pnl-tracking-error](2026-05-30-g3-live-pnl-tracking-error.md)），符合 D1「延後、非上線門檻」的判斷。
- 2026-09-25 `b17e0c4`：`Phase.CANARY` 退役（`BFX_PHASE=canary` 被拒），invariant 改名 `assert_live_guard_invariant` 並以 `live` phase 觸發，guard 集合隨 trading-state／envelope 模型調整；「真錢時必要 guard 必開，否則拒絕啟動」的核心沿用至今（`backend/ARCHITECTURE.md` §6）。

## Invariants

- 真錢 phase 下，必要 guard 任一未啟用 → daemon 不啟動（非零退出），不降級為 warning。

## Revocation Triggers

- 資金規模放大到人工監看跟不上 → 重評 D1，把自動 halt 升為上線門檻。

## Related

- 來源 spec：`2026-05-25-minimal-canary-enablement-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源 plan：`2026-05-25-minimal-canary-enablement.md`（原文已不在 repo，本 ADR 即紀錄）
- 相關：[2026-05-18-phase4-staged-rollout-paper-shadow-canary](2026-05-18-phase4-staged-rollout-paper-shadow-canary.md)、[2026-05-30-g3-live-pnl-tracking-error](2026-05-30-g3-live-pnl-tracking-error.md)、[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)、[2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md)。
