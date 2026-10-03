---
title: Postgres event-store 取代 Axiom 當 event-sourcing SoT（Hybrid 遷移）
date: 2026-05-23
status: active
tags: [bfx-funding-bot, decision, event-sourcing, postgresql, axiom, observability]
related-commits:
  - "47a6b8d^..5015424"
---

# Postgres event-store 取代 Axiom 當 event-sourcing SoT（Hybrid 遷移）

## Context

event-sourcing 的 source-of-truth 一直放在 **Axiom**（log-analytics SaaS）。這違反 repo 自己 phase-4.1 §Q9 立的分層原則（「log 平台做 diagnostics、relational DB 做 transactional invariants」）—— 2026-05-21 phase-4.2 ledger 決策把 transactional invariant 放錯層（drift）。同日 (5/23) Phase 4.4b deploy-verify 連環踩 2 個 Axiom wire-level bug（dot-notation project clause / columnar union schema null bleed，commits `03e3486` `0447e29`），加上 30d retention cliff、indexing latency（T12 polling 存在只為遮這個）、「Axiom down = daemon `replay_from_axiom` 失敗即 exit 1」硬耦合 —— Axiom 當 SoT 的痛點累積到臨界。pre-launch（無付費、無 real money），best-practice 重構成本最低時機。

## Options Considered

**SoT 模型**
- **A. Hybrid（選）** — append-only `event_log` + 交易內維護的 snapshot 表（`position_state` / `offer_claims`），cold start 一次 SELECT 不 replay
- B. 純 snapshot（B-strict）— 只 current-state 表，per-fill 覆寫
- C. Full event-sourcing（A-strict）— event stream 唯一真相、snapshot 僅 cache、runtime 可 replay

**寫入一致性**（Bitfinex REST 副作用無法進 DB txn）
- A1 observe-after（venue 為最終真相、事後記錄）/ **A2 write-ahead intent（選）**（cid 冪等 + venue reconcile）/ A3 full transactional outbox（背景 dispatcher）

**Axiom 診斷去留**（SoT 移走後只剩低量 forensic 事件）
- **整個移除（選）** — operational→structured stdout、forensic→PG `diagnostics` 表 / 自架 Grafana+Loki on k3s / keep Axiom diagnostics-only

## Decision

- **D1** SoT 模型 = Hybrid（event_log + 交易內 snapshot）
- **D2** Axiom 整個移除（operational→stdout、forensic→PG `diagnostics`）
- **D3** 寫入一致性 = A2 write-ahead intent（cid 冪等 + venue reconcile）
- **D4** 乾淨切換（空庫起步、開機對 venue reconcile、無 backfill）

## Rationale

- **D1**：money-handling 需 audit + bug 修好後重算 → 純 snapshot（B）丟歷史不行；但 daemon runtime 從不需走完整歷史才知現在部位 → full ES（C）的 runtime replay 是 over-engineering。Hybrid 取中：snapshot 給 O(1) cold start、event_log 給 audit/rebuild，**兩者同一 Postgres txn 寫入不可 diverge**（順手消滅現在 PG↔Axiom 無 txn dual-write 的整類 bug）。代價：比純 snapshot 多一張 log 表 + rebuild 邏輯。
- **D2**：SoT 移走後 Axiom 只剩低量診斷，Twelve-Factor「logs as stdout event stream」+ 把 forensic 丟已有的 Postgres（可跟 ledger JOIN）→ 一個 datastore、free-plan 上限問題消失、少一個網路依賴、§Q9 分層乾淨。不選自架 Loki：solo pre-launch 不需多養 infra（等上線當獨立決策）。代價：失去 Axiom query UI / dashboard。
- **D3**：money 路徑 canonical pattern 是「絕不在無耐久本地紀錄下對交易所送單」；cid 讓 crash 後能區分「從沒送出」vs「送了 ack 遺失」。不選 A1：正確性全壓在 reconcile poll 略弱。不選 A3：solo 單實例不需背景 dispatcher（repo `bus.py` 自己標 Phase 5+）。代價：多一個 PENDING 狀態機。
- **D4**：pre-launch + paper/shadow disposable data + Axiom 已在 evict → 一次性 backfill importer 是 YAGNI；開機對 Bitfinex 開倉 reconcile 即可安全重建。代價：捨棄歷史 realized-accrual（pre-launch 量級可忽略）。

## Result

- Plan 1（foundation，8 task TDD）已 ship + FF merge main：`git log --oneline 47a6b8d^..f1e81ef`（spec / plan / 10 impl）。
- 交付：`modules/execution/event_store/`（tables / serialization / store）+ alembic migration + `from_snapshot` cold-start loaders + `rebuild_snapshot_from_log`，**snapshot==rebuild 不變式有 property test**。與 daemon/Axiom 完全隔離（不動 `replay_from_axiom`，無 broken intermediate）。
- 驗證：698 unit + 5 integration green / mypy --strict clean / ruff clean。Final review **APPROVED_WITH_MINOR**（6 設計不變式全確認，0 Critical/Important；M1 account/env scope + M2 offer_claims rebuild 覆蓋已修）。
- migration 只在 throwaway container 驗 upgrade/downgrade，**故意未對 Neon 套用**（留 deploy）。

## Followup

- **Plan 2（cutover）** + **Plan 3（A2 + Axiom 全移除 + diagnostics）** just-in-time 寫（用 Plan 1 已具體的 store 簽章，避免 speculative drift）。
- Deploy-time prereq：(a) repo-root `.env` 損毀（`DATABASE_URL` 多行串一行，autogenerate 載不到）；(b) migration server_default `text("now()")` vs model `func.current_timestamp()` 對齊（對 Neon `alembic check` 前）；(c) migration 對 Neon `alembic upgrade head`；(d) `offer_claims` PK 為 `cid` 單欄，多帳號/PENDING 語意進來再評估 `(account_id, cid)` 複合 PK。
- **T12（同日已 ship `4aebb3a`→`8132ba8`）的 Plan 3 處置**（決定於 5/23，避免 Plan 3 誤刪 reusable）：**刪** Axiom-coupled（`AxiomConfig` / `AxiomClient` / `AxiomReplayQueryAdapter` 含 env filter / EventResource 的 Axiom-envelope 部分 / Axiom round-trip integration test (T7) / CI `AXIOM_CI_*` secrets + round-trip step / observability runbook 的 Axiom-dataset 部分）；**保留** generic（`DeploymentEnvironment` enum + `deployment_environment` tagging〔Plan 1 PG 表已用〕/ Dockerfile service-version (T6) / xfail markers (T8) / CI job 結構〔integration job 改跑 PG testcontainer 不需 Axiom creds〕/ dependabot (T10) / schema_version 概念）；T5 build_daemon wiring 由 **Plan 2 rework** 接 PG store。**T12 沒白做** — env-separation 思路直接餵了 Plan 1 schema 的 deployment_environment 欄位（見 O1）。

## Lessons

### Rules

- **R1**：對 serverless / shared DB（Neon）的 schema migration，**未 merge branch 階段絕不 `alembic upgrade head` 對線上 DB**。`Rule: autogenerate 產 file → throwaway container 驗 upgrade/downgrade → 線上套用 defer 到 deploy/merge`。
- **R2**：subagent 報「pre-existing failure / out of scope」時不可盡信。`Rule: controller 自己跑 canonical test gate（pytest -m "not integration"）驗證；實測常是 subagent 用非標準 invocation 的假象`（本次 3 fail 實為 integration test 跑在 sqlite）。

### Observations

- **O1**：同一天對 Axiom 從「強化」(T12 follow-up) 翻成「移除」(本遷移) 不是反覆 —— deploy 實戰把 §Q9 分層原則的成本/痛點具體化了。pre-launch「可重構到 best practice」framing 是 enabler。
- **O2**：implementer 兩次 out-of-plan 改動（Numeric `Mapped[float]`→`Mapped[Decimal]` money 精度 / sqlite dialect variant `_BIG_PK`/`_NOW`）方向都對 —— 好的 executor 會順手修 plan 沒考慮到的正確性問題。

## Updates (2026-05-23 — Plan 2 cutover SHIP + deploy HEALTHY)

**Plan 2 (cutover) shipped + deployed** — `git log --oneline e3b6c1d^..5015424`。boot 改走 `from_snapshot`(Postgres) 取代 `replay_from_axiom`,新增 `PostgresEventSink`(bus subscriber 持久化 SoT 事件,一 event 一 txn);刪 `Daemon.axiom_query` field。Followup 的 Plan 2 + (a)(b)(c) 全結案(見下);Plan 3 仍 pending。

**Sub-decision — deploy 失敗後選 cutover 而非 rollback/quick-patch**：push 觸 deploy 後連環踩 2 個 **T12(同日強化 Axiom)遺留**問題,非本遷移:(1) `BFX_DEPLOYMENT_ENV` 未設在 Koyeb shadow service(T12 manual-cutover 漏做)→ config_fatal exit 1;(2) T12 的 `where deployment_environment=='shadow'` APL 碰 legacy shadow rows 缺該 column → Axiom 400(CI ci-dataset fresh 所以過)。選項:**(A) 直接 Plan 2 cutover 永久修** / (B) rollback 到 pre-T12 `0447e29` / (C) quick-patch Axiom shadow 400。**選 A** —— rollback **不可行**:deploy 已把 Neon 推到 `4f8c2e91b3a7`,pre-T12 code 缺該 migration script → `alembic upgrade head` 失敗;quick-patch 是 throwaway(Plan 3 刪 Axiom)。代價:shadow 短暫 down(paper 無 real money 可接受)。結果:deploy `ace96a8c` HEALTHY,`daemon_started phase=shadow cells=11`,0 Axiom boot 依賴。

**Carry-forward 結案**:(a) `.env` line 1 三個 `DATABASE_URL=` 串接 → 從 `.env.chaos-bak` 還原 line 1(broken 存 `.env.broken-20260523-bak`);(b) migration server_default → `func.current_timestamp()` match model(`e3b6c1d`);(c) Neon migration 經 deploy 的 Dockerfile CMD `alembic upgrade head` 自動套用(Neon 現 rev `4f8c2e91b3a7`)。(d) `offer_claims` PK 仍 defer 到 Plan 3。

**新增 Rule**:
- **R3**:deploy entrypoint 自動跑 `alembic upgrade head`(Dockerfile CMD `alembic upgrade head && exec bfx-shadow`)→ push 即把 migration 套到 prod DB。`Rule: 評估「migration 何時/如何套到 prod」前先看 deploy entrypoint 是否 auto-migrate,別假設手動 defer`。連帶印證 O1(在 debug 正要刪的 Axiom 路徑 = 遷離方向正確)。

## Related

- 原 design spec（原文已不在 repo，本 ADR 即紀錄）：`docs/superpowers/specs/2026-05-23-postgres-event-store-sot-migration-design.md`（commit `47a6b8d`）+ Plan 1：`docs/superpowers/plans/2026-05-23-pg-event-store-foundation.md`（`f184d1b`）
  - 來源：spec 最終版 `2026-05-23-postgres-event-store-sot-migration-design.md`；Plan 1 `2026-05-23-pg-event-store-foundation.md`；Plan 2 `2026-05-23-pg-event-store-cutover.md`；3a-write plan `2026-05-24-pg-event-store-a2-write-path.md`（原文已不在 repo，本 ADR 即紀錄）。
  - 原「保留」理由（遷移進行中）已隨 3c 完成（2026-05-25）失效。
- 前序 ADR：[2026-05-23-phase4.4b-prework-cutover-enablers](2026-05-23-phase4.4b-prework-cutover-enablers.md)（本遷移 trigger 的 deploy bug 來源）、[2026-05-23-phase4.4a-bitfinex-live](2026-05-23-phase4.4a-bitfinex-live.md)
- 通用知識抽取：event-sourcing SoT layering / Hybrid（snapshot+log）/ Axiom-as-SoT anti-pattern 目前 Case 1 → 暫不抽，等第 2 案例

## Updates (2026-07-19)

- **Carry-forward (d) 結案（且是 stale 記錄，不是新完成）**：`offer_claims` PK 早於本 ADR 定稿隔天就已改成複合 PK（`account_id, deployment_environment, cid`，`f37f2b4` 2026-05-24，migration `c7d1e2f3a4b5`），Pending 清單卻沿用 ADR 當天「單欄 PK」的舊描述掛了兩個月。`last_event_seq` 恆 0 非遺留債——`store.py:192` 註解明寫 by-design（FSM state 是 claims SoT，high-water mark 由 position_state 扛）。教訓：Followup 段的完成狀態要跟著實作走，不能只在 code 改完當下同步一次就假設沒漂移。

## Amendment (2026-09-27): Plan 3 refinements 與 3a-write（spec §13 items 1、2、4、6）

壓縮 spec §13 與 3a-write plan 時補記原 ADR 沒寫到的四個 ruling（3a-recovery、3b、3c 各有自己的 ADR）：

### Amendment Decision

- **D5 = §13 item 1**：Plan 3 以 expand-contract（Strangler Fig）拆成 3a → 3b → 3c，contract（刪 Axiom）放最後；**而非**一次刪除，因為 Axiom 牽動 L3 smoke、signal_engine、health_monitor、fill_tracker、smoke_runner、g1 七處，遠超 §8 的清單。
- **D6 = §13 item 4（修正 Plan 2）**：SoT 持久化收進 command path 同步寫——txn1 `RESERVATION_INTENT`（PENDING）送單前 commit、REST、txn2 outcome（CLAIMED+FILL 或 `RESERVATION_FAILED`）commit，txn 絕不跨 REST call；Plan 2 的 async bus subscriber `PostgresEventSink` 退役，bus 降為純 in-memory projection + diagnostics fanout。**不選**保留 subscriber：fire-and-forget 是 projection 機制，拿來扛 SoT 是錯位（EventStoreDB／Marten 的正統是 append 在 command txn 內）；Plan 2 的 sink 本來就只是讓開機先脫離 Axiom 的過渡件。代價：middleware 變成持久化點，`ExecutorPort.submit` 多一個 `cid` 參數。
- **D7 = §13 item 2**：`offer_claims` PK 從裸 `cid` 改 `(account_id, deployment_environment, cid)`，投影改 cid-keyed 狀態機（INTENT→PENDING、CLAIMED、RELEASED、FAILED）；voi-keyed `transition()` 只留給記憶體 `OfferRegistry`。cid = blake2b(correlation_id+date) 不含 account，裸 cid 是唯一的多租戶破口；pre-launch 空表 migration 成本近 0。
- **D8 = §13 item 6**：cid 改由 middleware 以 capture-once 的 `submit_date` 先算好再傳給 executor，確保 INTENT 與 outcome 用同一個 cid（跨午夜不漂）。

**Result**：`git log --oneline 245b3aa^..a8eb543`（plan + 9 commits，migration `c7d1e2f3a4b5`，integration 覆蓋 intent-before-submit／same-cid／failed／crash-PENDING）。原 §6 的「PENDING 對 venue 查 cid + grace window」前提不成立（Bitfinex 不收 cid），由 [2026-05-24-event-store-3a-recovery-reconcile-by-venue-id](2026-05-24-event-store-3a-recovery-reconcile-by-venue-id.md) 取代；forensic 落點見 [2026-05-24-event-store-3b-forensic-diagnostics-in-postgres](2026-05-24-event-store-3b-forensic-diagnostics-in-postgres.md)；T12 的 env/CI 部分見 [2026-05-23-deployment-environment-realm-and-backend-ci-gate](2026-05-23-deployment-environment-realm-and-backend-ci-gate.md)。
