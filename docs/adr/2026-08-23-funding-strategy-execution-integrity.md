---
title: Funding 執行邊界採用 fail-closed typed gate 與 period-correct evidence
date: 2026-08-23
status: active
supersedes: "[2026-07-06-e2-book-aware-clamp](2026-07-06-e2-book-aware-clamp.md)"
tags: [bfx-funding-bot, decision, architecture, deployment, safety, observability]
related-commits:
  - "ec4b93a..e0e60df"
---

# Funding 執行邊界採用 fail-closed typed gate 與 period-correct evidence

## Context

2026-08-23 的設計檢查發現五個互相放大的缺口：WFO/OOS 可默默退回 linear fill estimate、live clamp 只看 scalar ticker、book 失敗會重用原 quote、diagnostics 不是 durable audit、optimizer 沒有禁止 submit 的型別邊界。這讓「沒有證據」與「可以下單」外觀相同。

既有 E2 決策（2026-07-06）仍以 ticker + scalar clamp 為核心；本次將執行邊界改成可驗證的 eligibility gate，並保留財務 event_log 與 execution audit 的責任分離。

## Options Considered

### 執行邊界
- **A. 維持 raw candidate → executor，錯誤時 fallback 原 quote**：改動最少，但無法在型別層阻止未驗證 submit。
- **B. typed eligibility gate（選用）**：以 ReadyToSubmit 才能呼叫 venue；成本較高，但 blocked/incomplete 可在資料結構中與 ready 分離。

### 市場資料與定價
- **A. scalar funding ticker + clamp**：延遲低、實作簡單，但不能證明候選的 exact period 或 amount competition。
- **B. 每次從 DB 重建 live strategy state / 延後觀察**：可增加 parity 或降低 race，但不能消除事後修訂，且增加延遲。
- **C. WS + REST reconcile 的 exact-period snapshot（選用）**：證據最完整，代價是 snapshot/sequence/checksum/reconcile 的狀態機與 blocking 行為。

### 證據與稽核
- **A. 保留 implicit linear fallback + best-effort logs**：離線流程容易繼續，但會把 evidence 缺失隱藏成 recommendation，重啟後也無法重建決策。
- **B. explicit empirical artifact + append-only audit（選用）**：需要 schema、scope/hash 與 write-ahead ordering，但能追溯「為何允許/阻擋」。

## Decision

- **D1**（spec §1）：executor 只接受 immutable ReadyToSubmit；BlockedExecution 與 NoRecommendation 不得觸發 venue call，移除 FALLBACK。
- **D2**（§2、§4）：live eligibility 使用有 freshness、symbol、sequence/checksum、exact-period、depth 證據的 MarketSnapshot；WS 為主要來源，REST 只做 reconcile，ticker 僅 telemetry。
- **D3**（§3、§4、§7）：以明確 ExecutionPolicy 區分 paper/book-guarded/optimizer-shadow/optimizer-live；empirical fill model 必須是有 scope/version/hash 的 artifact，linear-baseline 只可由明示的 offline command 使用；shadow optimizer 不得改變 submit rate。
- **D4**（§5）：每個 candidate 在 venue call 前寫入並 commit append-only execution_decisions；audit 失敗即 block；event_log 仍是 financial SoT，ReservationIntent 只帶 correlation。
- **D5**（§6、rollout）：events/metrics 使用 bounded labels；/healthz 保持 liveness，trading_ready 獨立表示交易資格；p14 只作 shadow profile，canary 不改。

## Rationale

- **D1**：把「可提交」變成能力型別，能從根本阻止 raw candidate 穿過 executor；相較在每個呼叫端補 guard，代價是一次性的 adapter/interface cascade，但漏接時會在編譯或測試階段暴露。
- **D2**：exact-period snapshot 才能回答「這筆 quote 在相同期間與數量是否有市場證據」；不選 ticker 是放棄簡單低延遲，換取不把錯期間的價格當成安全證據。不選只重建/只延後是接受 latency 卻仍留下事後修訂的 gap。
- **D3**：explicit artifact/blocked status 把 unknown 與 valid 分開；相較 implicit fallback，代價是資料不足時少放貸、離線流程要攜帶 model metadata，但避免以樂觀證據驅動 live rate。
- **D4**：write-ahead audit 讓重啟後可回答每次 submit 的依據；相較只寫 logs 或把 audit 混進 ledger，代價是一次 DB write 與 retention，但不污染 financial projection。
- **D5**：bounded observability 與 liveness/readiness 分離可避免高 cardinality 與因市場暫時不可用而 restart loop；代價是 operator 必須同時看 readiness、audit 與 logs。

## Result

- 完整實作範圍以 git log --oneline ec4b93a..e0e60df 取回；9 個 task 完成，branch 保持 local-only，沒有 remote deploy、live parameter change 或 resume call。
- cd backend_py && uv run pytest -m "not integration"：1876 passed, 86 deselected, 2 warnings；uv run mypy src/：183 source files 無錯；uv run ruff check：通過。
- fresh local PostgreSQL 執行 uv run alembic upgrade head 成功；uv run alembic check 回報 No new upgrade operations detected，另有 PostgreSQL integration test 1 passed。
- p14 仍 shadow-only；live optimizer、G3 ≥8 windows 與帳號遷移後人工核對仍是 promotion 前置條件。

## Followup

- G3 帳號遷移後窗口人工核對完成前，不提升 cap、不啟用 p14 canary 或 optimizer-live。
- /metrics 的 halted gauge/告警與 P3–P5 deployment hardening 仍待辦；本 ADR 不宣稱已完成。
- 下次 promotion 需保存 shadow evidence、L3 stability、corrected L4 結果與 operator approval。

## Invariants

- 任何 venue request 必須由已 audit-committed 的 ReadyToSubmit 發出。
- 缺少、過期、錯 symbol/period、sequence/checksum 無效或 depth 不足時，只能 block，不得重用原 quote、ticker inference 或 runtime linear fallback。
- /healthz 只代表 process liveness；交易資格由 trading_ready / readiness state 表示。

## Revocation Triggers

- exact-period book/reconcile 無法在 bounded latency 內提供可用證據 → 重評資料源與 timeout，但不得恢復隱式 fallback。
- daemon 變成 multi-instance/multi-tenant，或 audit identity/retention 不再足夠 → 重評 correlation、partition 與 break-glass 語意。
- empirical artifact 的 scope、point-in-time cutoff 或 confidence 無法再被驗證 → 停止 promotion 並重評 model contract。

## Amendment (2026-09-27): spec 中未入 Decision 的裁決

### Amendment Decision

- **D6（spec §4 optimizer）**：候選集固定為 signal rate、exact-period maker 價、exact-period taker 價（depth 足夠時）；score = `rate × fill_probability × (1 − fee)`；低於 strategy signal floor 的候選排除；同分時先選 fill 機率高的，再選 rate 低的。optimizer 一旦 arm，缺 evidence 就 block，不退回 signal rate。每個候選用自己價格的 evidence，見 [2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md) 2026-09-27 Amendment D7。
- **D7（spec §2.2、§5）**：WebSocket snapshot 本身仍新鮮有效時，REST backstop 的錯誤不單獨造成 block；spec 訂 `execution_decisions` 保留 365 天，與較短的 best-effort `diagnostics` retention 分開（2026-09-27 查 `modules/execution/audit/` 沒有 retention/prune 實作，這項尚未落地）；decision row 只存 snapshot 的 reference/hash 與 exact-period 證據，不複製整本 book。
- **D8（spec Non-goals，本輪明確不做）**：FRR floor promotion、固定的週末／spike 溢價、自動加 cap、AdaptivePeriod live activation、用自錄 snapshot 以外的資料重建歷史 book。

### Amendment Rationale

- D6 的同分規則偏向成交機率而非 rate：評分已把 fill 折算進期望值，同分時選較可能成交的，閒置風險較低。代價是極少數情況下讓出一點 rate。D7 的 REST 規則：REST 只負責 reconcile，把它的錯誤當成 block，等於讓次要來源否決主要來源的有效證據。

## Related

- Spec：`2026-08-23-funding-strategy-optimization-design.md`（原文已不在 repo，本 ADR 即紀錄）
- Plan：`2026-08-23-funding-strategy-execution-integrity.md`（原文已不在 repo，本 ADR 即紀錄）
- Implementation：git log --oneline ec4b93a..e0e60df
- Superseded ADR：[2026-07-06-e2-book-aware-clamp](2026-07-06-e2-book-aware-clamp.md)
- Commit convention evidence：git show 3bebccc:CLAUDE.md 與 git show 3bebccc:lefthook.yml；current amend e0e60df aligns with that existing policy。
