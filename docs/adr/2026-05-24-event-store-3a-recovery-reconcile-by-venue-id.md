---
title: Event-store 3a-recovery — boot reconcile by venue_offer_id (Bitfinex 無 client cid)
date: 2026-05-24
status: active
tags: [bfx-funding-bot, decision, event-sourcing, reconciliation, bitfinex]
related-commits:
  - "2175728^..495b35a"
---

# Event-store 3a-recovery — boot reconcile by venue_offer_id

## Context

PG event-store SoT 遷移 Plan 3 拆 3a/3b/3c。3a-write（A2 write-ahead intent）已 ship；3a-recovery 要補 boot venue reconcile + resolve crash-mid-flight PENDING + 接上 release/fill 的同步持久化（3a-write 退役 `PostgresEventSink` 後懸空，原 spec §13.1 item 7）。上游 ADR [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md) §6 假設「對每個 PENDING 的 cid 向 venue 查」——寫 plan 時 map 簽章 + 查 Bitfinex docs 發現該前提不成立。

## Options Considered

**crash-mid-flight PENDING 如何 resolve（cid 無法 round-trip）：**
- **A. reconcile-by-venue_offer_id（選用）**：以 venue 自己的 offer id 對帳；無法比對的 PENDING 收斂 FAILED。
- **B. 啟發式屬性比對**：用 (size, rate, period, mts≈INTENT) 把 venue offer 對回原 cid。
- **C. cid-on-submit**：把 cid 塞進 submit 靠 venue 回傳 — Bitfinex funding offer 不支援，不可行。

**release/fill 的 SoT 持久化：D. persist-then-publish at source（選用）** vs **E. 復活 Plan 2 async `PostgresEventSink`**。

**signed offers-query 放哪：F. 新 `BitfinexAuthREST`（選用）** vs **G. 掛在 public `BitfinexREST`**。

## Decision

- **D1（取代原 spec §6 step 3）**：reconcile-by-venue_offer_id（A）；PENDING 收斂 FAILED（capital-neutral）。
- **D2（補完原 §13.1 item 7 + 擴及 OrderFilled）**：persist-then-publish at source（D）。
- **D3**：signed query 用獨立 `BitfinexAuthREST`（F）。
- **D4**：orphan claim 合成 cid 用負數命名空間（`-int(voi)`）。

## Rationale

- **D1**：Bitfinex funding offer **submit 不收 cid、active-offers 回應也無 cid 欄**（index 20 是 placeholder；舊 `fill_tracker` `o[20]=cid` 是錯誤臆測、gated off 從沒 live 驗）→ cid 從未送達 venue，B/C 失效。業界 Reconciliation pattern：provider 無 idempotency-key round-trip（對比 Stripe）時以 provider 的 id 對帳，venue 為 offers 的 ultimate truth。**不選 B**：(size,rate) 相同會碰撞誤判，且 orphan-claim 已獨立把 capital 算回 → 啟發式是用 fragility 換邊際 audit 價值（anti-pattern）。**代價**：同一 offer 在 audit 上會有 FAILED(原 cid) + CLAIMED(合成 cid) 兩列。
- **D2**：event-store-append-in-command-path 是正統（EventStoreDB/Marten/Greg Young）；async fire-and-forget subscriber 扛 SoT 是錯位（原 §13.1 item 4 已退役）。**擴及 WS `fcn→OrderFilled`**：同一 sink-retirement gap，不補則上 live 後 realized 永不更新、reserved 只增不減。**代價**：persist 失敗時 ephemeral WS 事件的處理（fill_tracker 保留 voi next-tick 重試、ws_dispatcher skip-publish 不讓 in-memory 跑在 PG 前面）。
- **D3**：`BitfinexREST` 是 public client（api-pub、無 creds）；auth 需 api.bitfinex.com + HMAC → 獨立 client 比把 auth 混進 public client 乾淨。
- **D4**：真實 `generate_cid` 恆正（blake2b & MAX）→ 負數合成 cid 結構性零碰撞、deterministic、自帶「無 client intent」語意。**不選**reviewer 建議的 `1<<62`（真實 cid 可含 bit 62、`1<<63` 溢出 signed BIGINT）。

## Result

- `git log --oneline 2175728^..495b35a`（15 commits / subagent-driven 9 tasks / push origin/main）。
- unit 726 pass / integration 46 pass / mypy strict + ruff clean / **無 schema migration**（只讀寫既有 `event_log`·`offer_claims`·`position_state`，diff 法驗、未碰 Neon）。
- Final whole-feature review **READY TO MERGE**：double-count / boot-idempotency 逐路徑 trace 確認（PG 與 in-memory 各套用 delta 一次、re-boot 由 compute-layer state diff 結構性 idempotent、會計算術一致）。

## Followup

- **上 live 前必驗**：3a-recovery 只對 stub auth_rest + ephemeral docker PG 驗過 → 需對 real Bitfinex 帳號跑一次 venue reconcile（signed offers-query round-trip + orphan/missing/PENDING 三路徑實打）。
- 下一步 Plan `3b`（diagnostics 表 + sink，forensic 落 PG）→ `3c`（Axiom 全移除，依賴 3b 先就位）。

## Lessons

### Rules
- **R1**：第三方 API 大架構決策前先驗「① resource transition 後 client correlation id 是否保留 ② 該 resource 本身是否支援 client-id round-trip」。這次 §6 假設 ① 但漏 ②=否。`Rule: 寫 spike / 查 schema doc 確認 ID lineage 再 commit 架構`。
- **R2**：為「外部沒給 id 的實體」合成本地 key 時，放進真實 id 觸不到的命名空間（正/負號、保留 bit），別靠機率不碰撞；reviewer 給的命名空間建議本身也要 sanity check（`1<<62` 無效）。

### Observations
- **O1**：subagent-driven 中 verify subagent 跑 `git checkout <base>` 重現 bug → detach HEAD，後續 commit 變孤兒；controller 須在會 checkout 的驗證 task 後 `git symbolic-ref -q HEAD` 確認 attach。

## Related

- 原 spec §13.1 item 8 + plan `docs/superpowers/plans/2026-05-24-pg-event-store-a2-recovery.md`（commit `1432c5a`）。spec 為 3a/3b/3c 共用（原文已不在 repo，本 ADR 即紀錄）。
- 共用 spec 取回（§6、§13.1 items 5、7、8）：`2026-05-23-postgres-event-store-sot-migration-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 上游 ADR [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md)（本 ADR 是其 §6/§13.1 的 refinement）。
- 相關 lesson：Exchange APIs 跨 resource 弄丟 client correlation ID。通用知識未抽 — Bitfinex correlation-id 黑箱仍同 1 venue，接第 2 個 venue（Kraken/Binance）再抽。
