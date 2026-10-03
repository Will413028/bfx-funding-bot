---
title: Target architecture — shared control plane + account-isolated execution workers
date: 2026-08-31
status: active
tags: [bfx-funding-bot, decision, architecture, execution, saas]
---

# Target architecture — shared control plane + account-isolated execution workers

## Context

現行 real-money daemon 的 event log、write-ahead intent、single-writer submit 與 REST reconcile 骨幹正確，但 web/SaaS surface 已出現公開 signup、process-global account realm、daemon 不讀 user config/vault、同 VM backup 等邊界落差。產品尚無外部使用者，現在應先把自用實盤做成 production-grade，同時保留 host SaaS 的可演進接縫。

本決策細化 2026-06-06 frontend SaaS ADR 的 operator-console 與 execution-fleet 決策；不取代其 Better Auth、BFF、polling 等前端選型。

## Options Considered

### Operating scope

- **A. 立即開放 multi-tenant beta**：提早驗證需求，但把尚未完成的 account isolation、KMS、DR 與 profitability 風險交給外部資金承擔。
- **B. Operator-only，通過 gates 後再開外部帳戶（選用）**：先完成自用實盤完整性，代價是延後產品驗證。

### Execution topology

- **A. 維持 process-global account/env + 單一 daemon**：最省改動，但 auth user、vault、config 與 money realm 永遠無法一致。
- **B. Shared control plane + 每個 exchange account 一個隔離 worker（選用）**：共享 auth/config/reporting，execution 以帳戶切 blast radius。
- **C. 每租戶完整 stack/database + Kubernetes**：隔離最強，但在未知規模下帶來過高成本、部署與 incident surface。
- **D. 每位使用者 self-host**：平台不保管 key，但放棄 host SaaS 的低門檻、可觀測性與商業模式。

## Decision

- **D1**：目前產品強制 operator-only；signup server-side 關閉，operator route 以 immutable user id/role 授權。
- **D2**：採 bridge model：control plane 共用；execution plane 以 `ExchangeAccount` 為 aggregate root，每個帳戶同時最多一個持 lease 的 worker。
- **D3**：維持同 repo modular monolith，但 control plane、account worker、reporting 為不同 process/runtime boundary；現在不用 Kubernetes、Kafka 或 database-per-tenant。
- **D4**：初期共用 PostgreSQL；money tables 全帶 immutable account identity，control-plane commands 以 desired state + transactional outbox 交付，外部租戶前加 FORCE RLS/scoped roles。
- **D5**：config 改 immutable version + desired/applied acknowledgement；credential 只有一個 `CredentialProvider` SoT，禁止 env/vault 雙來源。外部 key 前改 KMS multi-version rotation。
- **D6**：public marketing/proof/CSV 使用預先產生的 static artifacts，不直接查 money database。
- **D7**：外部 real-money beta 必須同時通過 safety/data-integrity、economic non-inferiority、offsite DR restore、KMS/isolation、Bitfinex consent 與法律審查 gates。

## Rationale

- **D1**：相較立即 multi-tenant，先限制為 operator-only 可讓自己的 capped canary 承擔遷移風險；代價是延後 demand validation，但不能用客戶資金補足架構驗證。
- **D2**：exchange account 才是 credential、nonce、allocation、reconcile 與 blast radius 的自然邊界，而非 login user 或 tenant。相較 full silo，bridge 保留高風險執行隔離但共享低風險控制層；代價是要管理 worker lease/lifecycle。
- **D3**：未知規模時先建立 process/interface boundary，而非先買 Kubernetes 複雜度；代價是未來 scale-out 仍需 scheduler/deployment stamps，但不必重寫 money core。
- **D4**：PostgreSQL transaction/outbox 能原子提交 desired state 與命令，相較 Kafka 降低 solo ops；RLS 是 query-bug 的 defense-in-depth，代價是 policy/owner/BYPASSRLS 也要測，不能取代 application authz。
- **D5**：相較 inert CRUD 與 process-global env，versioned config/credential SoT 讓 UI 顯示真實 applied state 並支援 rotation；代價是 activation protocol 較複雜，但消除 silent drift。
- **D6**：公開流量與 money DB 分離可縮小 DoS、資料外洩與 DB failure blast radius；代價是資料有批次延遲，對週報型 proof 可接受。
- **D7**：自己的資金可用固定 cap 做 canary，但外部資金需要可驗證 recovery、隔離及策略證據；不選只靠「功能完成」作 launch gate。

## Expected Outcome

自用階段得到可復原、權限閉合、帳戶身份一致的 real-money system；未來第一個外部帳戶只需自動 provision 新 account worker，而不用重寫 event-sourced execution core。成功指標是無 global realm authorization、無 ambiguous submit 被當 failed、projection 可決定性重建、RPO ≤5m 且 restore drill RTO ≤60m。

## Amendment (2026-09-22)：D4 的 outbox 適用於所有 operator 裁決動作

### Amendment Context

2026-09-21 canary 送單遇 Bitfinex HTTP 500，系統依 I-WAI 開 `submit_outcome_unknown` 並擋住 fUST。
operator 經 webapi `mark-not-accepted` 裁決時連撞五張表的 `permission denied`（`event_log`、
`event_log_event_seq_seq`、`event_prefix_hashes`、`projection_heads`、`execution_uncertainties`），
全被包成同一個 `resolution_rejected` 409，真正原因只留在 postgres log。

根因不是漏授權：`AccountEventWriter.append` 同時 append event 與**同步套用 projection**，
呼叫它的 process 因此需要該事件影響的全部 read model 寫權限；而 `bfx_webapi` 每張表都是唯讀 `r`，
那是刻意的。這條路徑**在 production 從未成功執行過一次**。D2/D3 已定 control plane 與 execution
plane 為不同 process boundary、D4 已定 control-plane commands 走 outbox——實作沒有遵循。

### Amendment Options

- **A. 對齊 D4 的 outbox（採用）**：webapi 只寫請求欄位，單一 daemon worker 消費後才 append 與
  project，與既有 ReleaseSessions 同型；代價是要改程式與 migration，且裁決由同步變非同步。
- **B. 繼續補 GRANT**：今天就能動；代價是每多一種 operator 裁決事件就可能再撞一次，webapi 逐步
  取得整個 ledger 寫權限，D2/D3 的 process boundary 名存實亡。
- **C. 把 projection 改成非同步下游**：最接近純 event sourcing，`projection_heads` 已在追 lag；
  代價是 read-after-write 一致性消失，金融操作介面按下裁決後看到舊狀態。

### Amendment Decision（D4'，extends D4）

- **D4'**：outbox 不限租戶 control-plane，**所有 operator 裁決動作**一律走「webapi 寫請求 →
  單一 daemon worker 執行」；webapi 對 ledger 與 projection 表維持**零寫權限**。
- **授權必須可重現**：role grant 改由 migration 或受版控 SQL 提供，並加 CI 斷言「webapi 對 ledger
  表無寫權限」，讓違反在 CI 失敗而非在 production 撞 500。
- **過渡**：2026-09-22 為解 canary 阻塞已手動 GRANT 上述五項；D4' 落地時**必須回滾**。回滾前
  這些 grant 只存在該 VM 的資料庫，不在任何版控產物裡。

### Amendment Rationale

- **選 A 而非 B**：B 解症狀。真正成本不是這次五張表，而是往後每個裁決動作都要在 production
  重新發現一次同樣的邊界；A 讓新增裁決動作不再需要任何新 GRANT。
- **選 A 而非 C**：C 解更上游的問題但拿掉即時一致性，對「按下裁決→看到結果」是退步；A 已足以
  修復 process boundary，不必同時改動 projection 語意。
- **放棄了什麼**：裁決由同步變非同步，UI 需顯示 pending；ReleaseSessions 的四步流程本就是此型態。

### Amendment Revocation Triggers

- worker tick 延遲使 operator 在時間敏感裁決（如 DR 900s 窗口內）無法及時完成。
- 出現必須由 control plane 同步寫入 ledger 才能滿足的法規或稽核要求。

## Followup

- 完成 P0 containment：關 signup、撤銷非 operator sessions、補 operator/account authz。
- 完成 money integrity：UNKNOWN submit lifecycle、per-account serialized projection、orphan quarantine。
- 建立 offsite WAL/PITR、月度 restore drill、readiness 與主動 alerts。
- 引入 ExchangeAccount、config versions/outbox、CredentialProvider 與 worker lease。
- 收斂 OpenAPI contract、immutable image deploy、expand/contract migrations；外部 beta 依 D7 gates 解鎖。

## Invariants

- Control plane 不直接呼叫 Bitfinex money-write API。
- 一個 ExchangeAccount 同時最多一個 execution writer，且 worker 只能取得該帳戶 credential。
- 所有 submit 先 durable intent；無明確拒絕證據時為 UNKNOWN、不得自動重送。
- Venue reconcile 是 exposure authority；未知 venue object 仍計入 exposure 並只 block 相關 account/symbol。
- Desired config 不等於 applied config；未 acknowledgement 不得宣稱已生效。
- Public plane 不可直接讀 credential、event log 或 live money projections。

## Revocation Triggers

- 單一 deployment cell 無法達成容量、RTO 或故障隔離目標時，重評 deployment stamps/Kubernetes。
- Shared PostgreSQL 即使 RLS/scoped roles 仍無法滿足外部租戶隔離要求時，重評 schema/database silo。
- Bitfinex terms、監管或法律意見不允許 hosted delegated execution 時，重評 self-host/partner model。
- Economic non-inferiority 長期未達成時，停止外部 execution productization，不以架構完成替代策略證據。

## Related

- 本 ADR 即 2026-08-31 使用者與 coding agent 架構 review 的原始決策紀錄，無外部來源文件。
- [2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md) — operator console、Better Auth/BFF 與 execution fleet 的前置決策；本 ADR 細化 execution topology。
- 2026-05-26-bfx-productization-host-saas（產品方向決策，不在本 repo） — host SaaS 與準 non-custodial 產品方向。
- [2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) — money path execution gate 與 audit invariants。
- [2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md) — REST reconcile 作 correctness backbone。
- Industry reference: [AWS SaaS Lens — bridge model](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/bridge-model.html).
- Industry reference: [PostgreSQL row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) 與 [continuous archiving/PITR](https://www.postgresql.org/docs/17/continuous-archiving.html).
- Followup 前兩項（P0 containment、money integrity）的實作決策：[2026-08-31-production-integrity-staged-clean-cutovers](2026-08-31-production-integrity-staged-clean-cutovers.md)（分段 clean cutover、UNKNOWN、serialized projector，`7678f93^..09761bc`）；Halt 2 recovery：[2026-09-10-historical-replay-and-projection-audit-cutover](2026-09-10-historical-replay-and-projection-audit-cutover.md)。
- 2026-09-25 D4' 已落地並部署（PR #17 `d475d51`；outbox 分支原 commit `4e99d6f`）：operator 裁決與 trading control 共用一套 operator request outbox，migration `1c435a35dcb4` 收回五項手動 GRANT，webapi 權限改由 migration `c74d45a54e46` 提供。

## Updates (2026-09-28)

- [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 以 bot 事實 insert-once journal＋venue 已接受觀測取代 event-only projector 與「projection 可決定性重建」；per-account lock、四值 outcome、UNKNOWN、orphan 與禁止盲目 restore 的決定不變。
