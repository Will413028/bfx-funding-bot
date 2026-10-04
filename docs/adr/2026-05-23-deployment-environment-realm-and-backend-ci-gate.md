---
title: deployment_environment 資料 realm 標記 + backend CI gate（T12，pre-real-money）
date: 2026-05-23
status: active
tags: [bfx-funding-bot, decision, ci, observability, axiom, environment-separation]
related-commits:
  - "64e2e4d^..8132ba8"
---

# deployment_environment 資料 realm 標記 + backend CI gate（T12）

## Context

2026-05-23 Phase 4.4b prework 上 prod 後連環踩 2 個 Axiom wire-level bug（`03e3486` APL project clause 400、`0447e29` columnar union schema null bleed）：唯一能抓它們的 T12 real-Axiom round-trip test 因 CI 沒設 creds 被 `skipif` 跳過，且 CI 根本沒有 Python backend job（連 unit 都沒在 PR 跑）。同時 shadow service 寫進 prod dataset，canary 開始後 real-money 事件會與 paper/shadow 混在一起（G3 被稀釋、alert 無法區分）。prior state：單一 Axiom dataset、無 env 欄位、CI 只有 Go/frontend。

**約束**：

- `external` 小額 real-money canary 即將開始：wire-level bug 不能再靠上 prod 才發現。
- `external` Axiom free plan 限制 dataset 數量；Axiom 不支援 OIDC，只能用 long-lived dataset-scoped token。
- `inherited` Axiom 當時是 event-sourcing SoT（[2026-05-23-phase4.4a-bitfinex-live](2026-05-23-phase4.4a-bitfinex-live.md)）；當天稍晚即由 [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md) 推翻，所以本 ADR 中綁 Axiom 的部分已不成立，只剩 vendor-neutral 的部分仍有效。

## Options Considered

**環境隔離**
- **基準. OTEL resource attribute `deployment.environment.name` + 每 env 各自的資料分區與 scoped credential**：[OpenTelemetry semantic conventions — deployment environment](https://opentelemetry.io/docs/specs/semconv/resource/deployment-environment/)；Datadog `env` tag／Honeycomb dataset-per-env 同型。
- **A. 維持單一 dataset、不標 env**：零成本，但 real-money 與 shadow 混流。
- **B. 3 個實體 dataset（prod/shadow/ci）+ dataset-scoped token + emitter/reader 雙端 `deployment_environment` 標記（選用，= 基準）**。
- **C. 等上線後再分**：之後要做 dataset migration，風險更高。

**CI 觸發與隔離**
- PR + main push 都跑（選用）／只跑 main／nightly。
- 並行 run 隔離：run-scoped `account_id=ci-{run_id}-{sha}`（選用）／mutex 串行化 PR queue。
- Axiom indexing 等待：polling with timeout（選用）／固定 `sleep(5)`。

**既有 5 個 SQLite/JSONB integration failure**：`xfail(strict=False)`（選用）／本 spec 一起修／`skip`。

## Decision

- **D1 = spec D1–D6**：emitter 在 envelope 頂層注入 `deployment_environment`，reader 以同一值過濾（雙端對稱，`build_daemon` 單元測試鎖）；env var 用 vendor-neutral `BFX_DEPLOYMENT_ENV`，`DeploymentEnvironment` StrEnum `{prod, shadow, ci}`，缺值 fail-loud。
- **D2 = spec D8–D11、D21**：CI 新增 backend unit + integration job，PR 與 main push 都跑；PR `cancel-in-progress`、main push 不 cancel；integration `needs` unit；fork PR 跳過 integration；workflow／job `permissions: contents: read` + `timeout-minutes`；Dependabot 每週升 Actions pin。
- **D3 = spec D12、D13**：run-scoped `account_id` + 每事件 uuid correlation；polling 取代固定 sleep。
- **D4 = spec D15、D14**：既有 failure 用 `xfail(strict=False)` 標，不混修；Pydantic `extra='ignore'` 不 bundle。
- **D5 = spec D3/D18–D20**：OTEL-inspired `EventResource`（`service_version`=build-time `GIT_SHA`、`schema_version=1`、optional `host_name`、lazy `to_otel_resource()`），不現在導入完整 OTEL SDK。

## Rationale

- **D1**：pre-real-money 是分層成本最低的時機，**而非** C 等上線後做 migration；**不選** A：G3 與 alert 會被 shadow 噪音污染。env 是通用概念，不綁 vendor 前綴（`AXIOM_`），所以 Axiom 移除後標記仍可直接沿用。**代價**：free plan 做不到 B 的實體三分，落地改成 2 dataset（prod+shadow 共用、ci 獨立）＋邏輯標記（`8132ba8`）；隔離強度從「物理上不可能寫錯」降為「靠查詢過濾」。
- **D2**：wire-level bug 在 PR 階段抓最便宜；PR cancel 省 quota、main 不 cancel 保住 deploy 紀錄；unit 先 fail 就不浪費 integration。**相較** nightly，回饋延遲太長。**代價**：CI 時間與 secret 管理成本。
- **D3**：mutex 會讓 PR 排隊；run-scoped id 讓並行 run 自然不互撞。固定 sleep 在 2–15s indexing latency 下會 flaky 或拖慢。
- **D4**：修 JSONB/SQLite 不相容是另一件 yak shave；`strict=False` 讓「意外修好」浮出成 warning 而非靜默。
- **D5**：欄位名對齊 OTEL semantic convention，未來 OTEL migration 可一對一映射，**不選**現在導入 SDK（2–3 天、無即時需求）。**代價**：多一個自有 dataclass。

## Result

- `git log --oneline 64e2e4d^..8132ba8`：spec、plan、11 task 與 free-plan 2-dataset runbook 變體。
- **同日被部分推翻**：[2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md) 決定整個移除 Axiom；[2026-05-25-event-store-3c-axiom-removal](2026-05-25-event-store-3c-axiom-removal.md) 刪除 `EventResource`／Axiom dataset／T12／`AXIOM_CI_*` secret。**仍存活**（origin/main 2026-09-27 核對）：`BFX_DEPLOYMENT_ENV` 必填且 fail-loud（`core/settings.py`）、所有 SoT 表的 `deployment_environment` 欄、`.github/workflows/ci.yml` 的 PR/main 觸發＋PR-only cancel＋`needs`＋`permissions`/`timeout-minutes`、`.github/dependabot.yml`、Dockerfile 的 build-time `GIT_SHA` 版本標記。envelope 層 `schema_version` 隨 `EventResource` 一起刪除（domain event 自己的 `schema_version` 來自 4.4a，不是本 ADR）。
- 2026-05-26 `e1ddb65`：canary 的 runtime 標成 `shadow` realm（deploy 腳本沒設 `BFX_DEPLOYMENT_ENV`），修成 phase 與 realm 正交且 `load_config` 做 fail-fast 配對檢查——D1「realm 必須顯式」的直接延伸。

## Revocation Triggers

- 引入正式 observability 平台（OTEL exporter／Loki／SaaS）→ 重評 realm 標記的欄位名與 CI 隔離方式。
- 出現多租戶以外的第四種 realm（例如 staging）→ 重評 `{prod, shadow, ci}` 列舉。

## Lessons

### Rules

- **R1**：spec 的 acceptance 寫「real backend 驗證」，但測試被 env-gated skip，等於沒驗。`Rule: CI 對 real-backend test 的 skip 要可見（xfail／明確 job），不可讓缺 creds 靜默變成綠燈`。

## Related

- 來源 spec：`2026-05-23-axiom-ci-env-separation-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源 plan：`2026-05-23-axiom-ci-env-separation.md`（原文已不在 repo，本 ADR 即紀錄）
- 觸發：[2026-05-23-phase4.4b-prework-cutover-enablers](2026-05-23-phase4.4b-prework-cutover-enablers.md) Followup「HIGH — T12 真跑起來」與 R4。

## Amendment (2026-10-04): unit 與 integration 並行

### Amendment Context

D2 讓 integration `needs` unit，失敗的 unit 不會再花 integration 的時間；代價是 CI 牆鐘時間是兩者相加（unit 約 4.5 分加 integration 約 13.6 分）。repo 是 public，Actions 分鐘不計費，省下的只剩 runner 時間；CI 結果是完整 integration 的唯一依據（本機只跑受影響的檔案），等待時間直接卡住每個 PR。

### Amendment Decision

- integration 與 deploy gate 不再 `needs` unit，三者並行；unit 用 `pytest -n auto`，integration 用 `-n 4 --dist loadfile`（4 vCPU runner）。
- integration 的 DB 改為每個 pytest process 一台本機 PostgreSQL 18（PGDG apt），major 與 `deploy/vm/postgres/Dockerfile` 一致；需要 Docker 的測試標 `docker`，CI 上 `BFX_REQUIRE_DOCKER=1` 使其不得 skip。
- D2 其餘部分（觸發、PR-only cancel、fork 跳過 integration、permissions、timeout、Dependabot）不變。

### Revocation Trigger

repo 轉為 private（Actions 分鐘計費）或 unit 失敗率高到並行的 integration 浪費明顯時，恢復 `needs`。
