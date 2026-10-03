---
title: Event-store 3b — forensic 診斷事件落 Postgres `diagnostics` 表（乾淨切換、best-effort 獨立 txn）
date: 2026-05-24
status: active
tags: [bfx-funding-bot, decision, event-sourcing, observability, axiom, postgresql]
related-commits:
  - "73a640e^..5f6f16f"
---

# Event-store 3b — forensic 診斷事件落 Postgres `diagnostics` 表

## Context

PG event-store SoT 遷移（[2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md) D2：Axiom 整個移除）拆成 3a／3b／3c。3a 把 SoT 寫入搬進 `event_log`；剩下的 Axiom emit 分兩類：forensic（DECISION、SAFETY_TRIGGER、CANCEL_AUDIT，事後查因果鏈用）與 operational（SIGNAL、HEALTH_CHECK、ORDER_SUBMIT attempts）。3b 決定 forensic 的去處與寫法，3c 再處理 operational 與刪除。

**約束**：

- `external` pre-launch：paper/shadow 資料可丟，沒有線上不可停機的遷移壓力。
- `inherited` 正式 observability 平台延後到上線再決定（上游 ADR D2）；現在仍成立：solo、無付費用戶。
- `inherited` 分層原則「diagnostics 失敗絕不阻斷交易」（上游 spec §10）；仍成立，這是 money path 的安全前提。

## Options Considered

**forensic 落點**
- **基準. Twelve-Factor XI：一律寫 stdout，交給平台 route**：[12factor.net/logs](https://12factor.net/logs)
- **A. PG `diagnostics` 表（選用）**：`id`、`account_id`、`deployment_environment`、`kind`、`payload` JSONB（整個 event lossless）、`occurred_at`、`recorded_at`；index `(account_id, occurred_at)`；可 prune 30–90d。
- **B. 自架 Loki／SaaS**：上游已延後。

**切換方式**：乾淨切換（選用）／Axiom 與 PG 雙寫 + shadow-read。

**sink 介面**：drop-in `emit(dict)`，與 `AxiomClient` 同一個 port（選用）／新簽章 `record(Envelope)` 並做完整 `Envelope.model_validate`。

**txn 與可靠性**：獨立 `session_scope`、單次嘗試、失敗 swallow + 記 stdout（選用）／共用 command txn／in-process buffer + retry。

**事件進入路徑**：DECISION、SAFETY 直接 emit，cancel 走 bus subscriber（選用）／全部硬塞進 DomainEventBus。

## Decision

- **D1 = spec §13.1 item 9**：forensic 三類落 PG `diagnostics` 表；只收 `DECISION`／`SAFETY_TRIGGER`／`CANCEL_AUDIT`（`CANCEL_REQUESTED`+`CANCEL_ACKNOWLEDGED`），其他型別 drop；HEALTH_CHECK／SIGNAL／ORDER_SUBMIT 屬 operational，留給 3c 走 stdout。
- **D2**：乾淨切換，不雙寫。
- **D3**：`DiagnosticsSink.emit(dict)` 與 `AxiomClient` 同 port，寬鬆抽取 `event_type`／`account_id`／`timestamp`／`payload`；另有 `NoopDiagnosticsSink` 給 chain test。
- **D4**：`_insert` 一律開自己的 session/txn，絕不共用 command txn；best-effort 單次、swallow、無 buffer/retry。
- **D5**：DECISION／SAFETY 直接 emit；cancel 由 bus handler 從 domain event 組 payload，兩路收進同一個 `_insert`。
- **D6**：不加 `correlation_id` 欄與 index（用 `payload->>'signal_correlation_id'`）；模組獨立放 `modules/execution/diagnostics/`，不塞進 `event_store/`。

## Rationale

- **D1**：**相較**基準的純 stdout，建表的兩個理由缺一不可：要在 SQL 裡跟 `event_log` JOIN（signal → decision → reservation → fill 因果鏈），且平台尚未引入、平台 log retention 短。定位是過渡用的 forensic store，**不做成迷你 observability 系統**。**代價**：多一張表與 migration。ORDER_SUBMIT 的耐久事實已在 `event_log`（INTENT/CLAIMED/FAILED），剩下的 attempts/latency 是 telemetry。
- **D2**：雙寫 + shadow-read 是為線上、有真實資料、不能停機的遷移而存在；這裡只會讓失敗面加倍，還要處理「Axiom 成功、PG 失敗」的一致性。3b／3c 仍拆開，理由是 diff 體積，不是資料 shadow。
- **D3**：同 port 讓 3c 刪 Axiom 變成純刪除、零介面落差。**不選**完整 `Envelope` 驗證：它的「非 HEALTH_CHECK 須帶 strategy+cell」是上游約束，會誤丟 smoke-boot 合法的 `strategy=None` SAFETY_TRIGGER。**代價**：型別保證弱，payload 形狀不一（smoke-boot 與 safety chain 兩種 shape，3c D4 決定不正規化）。
- **D4**：唯一的 correctness invariant 是 diagnostics 失敗不可 rollback SoT 寫入，所以**不選**共用 command txn。**不選** buffer/retry：寫的是與 SoT 同一個 PG，PG 掛時 SoT 也掛（相關失敗），buffer 在 crash 時一樣丟。**代價**：PG 短暫故障時 forensic 會漏。
- **D5**：cancel 本來就是 order-lifecycle domain event，DECISION／SAFETY 是決策 telemetry、不是 state transition；硬塞進 bus 會汙染「reservation 生命週期事件」的語意。
- **D6**：solo bot 千列量級，無索引 JSON 查詢已足，加欄位是 premature optimization；非 SoT、非 append+projection，放進 `event_store/` 會混淆邊界。

## Result

- `git log --oneline 73a640e^..5f6f16f`：plan + 14 impl commits，migration `d8e3f1a2b4c6`。741 unit + 47 integration 綠、mypy／ruff clean、`alembic upgrade head` 過。
- 3c（[2026-05-25-event-store-3c-axiom-removal](2026-05-25-event-store-3c-axiom-removal.md) O1）驗證 D3 預測：operational repoint 全是機械改名，emit payload byte-identical。
- 2026-07-07 E3 部署踩到 D3 寬鬆抽取的副作用：`account_id` 以 `event.get(..., "default")` 取值，真實 decision 不帶 → 跨表 JOIN 全空；改成 sink 建構時注入權威 account（`215c0ce`，[2026-07-07-e3-measurement-and-diagnostics-realm-authority](2026-07-07-e3-measurement-and-diagnostics-realm-authority.md)）。

## Revocation Triggers

- 引入正式 observability 平台 → diagnostics 表降級或退役。
- 列數成長到 JSON 無索引 JOIN 變慢 → 重評 D6 的 `correlation_id` 欄。

## Related

- 來源 spec（§4 `diagnostics`、§10、§13.1 items 9–10）：`2026-05-23-postgres-event-store-sot-migration-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源 plan：`2026-05-24-pg-event-store-3b-diagnostics.md`（原文已不在 repo，本 ADR 即紀錄）
- 上游 [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md)；後續 [2026-05-25-event-store-3c-axiom-removal](2026-05-25-event-store-3c-axiom-removal.md)。
