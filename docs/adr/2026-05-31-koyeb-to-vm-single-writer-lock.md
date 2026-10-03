---
title: Koyeb → 自管 VM 遷移：以 Postgres advisory lock 取代平台的單實例保證，migrate 拆成 one-shot
date: 2026-05-31
status: active
tags: [bfx-funding-bot, decision, deployment, safety, postgresql, container]
related-commits:
  - "ca34e65^1..ca34e65"
  - "7cee591^1..7cee591"
  - 9dd1077
---

# Koyeb → 自管 VM 遷移：以 advisory lock 取代平台單實例保證

## Context

prior state：真錢 canary 跑在 Koyeb（Docker-from-GitHub、隱含單一實例、手動部署），DB 在 Neon、另有 Upstash Redis。2026-05-31 決定搬到自管的 Oracle Cloud Always-Free ARM VM（Tokyo，4 OCPU / 24GB）。13-agent 對抗式理解後確認：**Koyeb 的隱含單實例是歷史上唯一的並行寫入防線**——funding submit 沒有 cid／idempotency，每輪 cid 是新 uuid4、venue 給不同 offer id，event_log dedup 不會觸發；兩個 reconciler 會各自絕對覆寫 `position_state` 並各自補單超過 cap。搬到 VM 等於拿掉這道防線。

**約束**：

- `external` 真錢已在跑（小額 canary cap），遷移期間不能出現兩個 prod realm 寫入者。
- `external` Bitfinex 無法驗證 fencing token：venue 端沒有地方拒絕「舊 leader」的請求。
- `external` 平台換成 compose：`restart:` 只對 process exit 反應，不處理「活著但 unhealthy」。
- `inherited` Dockerfile CMD 為 `alembic upgrade head && exec bfx-shadow`；遷移當下 Koyeb 仍靠它跑，所以不能改（已不成立：Koyeb 已退役，CMD 保留只是無害歷史）。

## Options Considered

**單一寫入者**：
- **基準. lease + fencing token**（Kleppmann,「How to do distributed locking」, 2016）：資源端驗證遞增 token，拒絕過期 leader。
- **A. session advisory lock + 每筆送單前 fail-closed 重驗（採用）**：開機 `pg_try_advisory_lock`，拿不到就退出；送單前在同一條連線驗證鎖仍在。
- **B. 只靠部署紀律**（stop-old-then-start-new）不加程式防線。

**migration 執行位置**：A. 沿用 inline `alembic upgrade head && exec` 於 `restart: unless-stopped`；B. **one-shot `migrate` compose service，bot `depends_on` 其成功完成（採用）**。

**unhealthy 重啟**：A. 只用 compose `restart:`；B. **`autoheal` sidecar＋debounce（採用）**。

**遷移範圍**：A. 同時調 cap／區域／Redis；B. **只換平台，其他風險維度不動（採用）**。

## Decision

- **D1 = spec #1＋plan 決策 1–7**：單一寫入者用 **session-level advisory lock**，放在**專用 NullPool 連線**（pool 回收會釋放鎖；`-pooler` 會被剝掉，txn-mode pooler 上 advisory lock 無效）；只在 live 路徑取得（paper／shadow 不搶鎖）；拿不到 → `WriterLockUnacquired` → exit 75（EX_TEMPFAIL）；`WriterLockGuard` 每筆真錢送單前**實際在該連線 `SELECT 1` 驗證**，不讀快取旗標；背景 30s liveness 只做重取與觀測。lock key = `blake2b("bfx-writer:{account}:{env}")` 取 signed int64，**禁用內建 `hash()`**（PYTHONHASHSEED 會加鹽）。
- **D2 = spec #4＋plan 決策 8**：migrate 拆成 one-shot service；`alembic/env.py` 另用**不同 namespace** 的 advisory lock 序列化並發 migration，`lock_timeout=5s`／`statement_timeout=60s`；**永不自動 `alembic downgrade`**，回滾靠 DB 還原。
- **D3 = spec #5**：`restart: unless-stopped`＋`autoheal`，healthcheck 90s 連續失敗才重啟、`start_period` 180s 防開機抖動；8080 不對外 publish。
- **D4 = spec #2/#3/#6/#7**：遷移時 cap 維持不變、`realized_loss_24h` 由舊的絕對額重算為 cap 的 10%、保留 Tokyo VM＋Neon Singapore（60–80ms，DB 不在下單關鍵路徑）、canary 不帶 Redis（`src/` 無 consumer）。
- **D5 = spec §B2/§C**：每個 phase 一份 env 檔，canary 專屬變數在 paper／shadow **不存在而非空白**；cutover 順序 paper（ci realm）→ shadow（shadow realm，可與 Koyeb 並行）→ **Koyeb scale 到 0 並在 Bitfinex 確認無掛單** → 起 VM canary；任一時刻只有一個 prod 寫入者。

## Rationale

- **D1 選 A 而非基準**：fencing 的保護來自資源端驗 token，Bitfinex 沒有這個能力，所以 lease＋fencing 在這裡只剩 lease，代價是多一套租約續期卻沒有多擋任何一筆重複送單。session lock 綁在連線生命週期上，連線斷＝鎖失；把重驗放在送單前，重複送單的窗口被壓到接近零——這才是真正保護 venue 的那一步。**不選 B**：部署紀律擋不住誤操作或 supervisor 重啟時的短暫雙開。代價：多一條常駐 DB 連線，且 DB 不可用時 live 直接停止送單。
- **D1 為何「live verify」而非旗標**：背景旗標在兩次心跳之間會過期，恰好是鎖剛遺失的那段時間。
- **D2 選 B**：inline migrate 在壞 migration 時會讓交易 process crash-loop 並每次重跑；當時 `b7c1d2e3f4a5` 會 DROP 重建 `position_state`。downgrade 有損，不可作為自動回滾。
- **D3 選 B 而非只用 compose**：deadlock 但仍回 503 的 daemon，compose 不會重啟，比 Koyeb 更差；debounce 的代價是最長約 90s 的偵測延遲，換到 WS／DB 短暫抖動不會重啟真錢 daemon。
- **D4**：一次只改一個風險維度；cap 放大是遷移後的獨立決策（當天稍晚即調高 cap，`f27ce9a`）。
- **D5**：`BFX_EXECUTOR` 預設 `paper`、`BFX_CELLS_YAML` 缺省會載入雙幣別設定，空白值與缺值行為不同，所以「不存在」才等同 Koyeb 的 `!VAR` 刪除語意。

## Result

- `git log --oneline ca34e65^1..ca34e65`（hardening：writer lock、migration lock、limiter）與 `git log --oneline 7cee591^1..7cee591`（compose、per-phase env、`deploy-vm.sh`），`9dd1077` 修 healthcheck（slim image 無 wget → python）。
- 2026-05-31 cutover 完成（ARCHITECTURE §8：「Koyeb 已於 2026-05-31 cutover 至 VM」）。
- 仍在 main：`core/writer_lock.py`、`WriterLockGuard`、`WriterLockWatch`、`alembic/env.py` 的 migration lock、compose `autoheal`。
- 已被取代：D4 的 cap 與絕對額 limiter（`0b90763` 改 % of NAV，後由 [2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) 取代）；D5 的 `canary` phase 已移除；Neon 於 2026-06-23 退役（見 [2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md)）；`deploy-vm.sh` 在 VM 上 build 的方式由 [2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md) 取代。

## Invariants

- 任一時刻 prod realm 只有一個持有 writer lock 的 live process；拿不到鎖的 process 必須在碰 venue 前退出。
- 真錢送單前的鎖驗證必須是對專用連線的即時查詢，不得改成讀快取狀態。

## Revocation Triggers

- venue 開始支援 client order id 冪等或可驗證 token → 重評 D1，改用 venue 端去重。
- 改成多實例或需要 HA failover → session lock 的「連線斷即失鎖」語意需重評。
- DB 前面加上 transaction-mode pooler → session advisory lock 失效，D1 必須改。

## Related

- 來源（Provenance）：
  - spec：`2026-05-31-koyeb-to-vm-migration-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - plan A（hardening）：`2026-05-31-bfx-hardening-for-self-host.md`（原文已不在 repo，本 ADR 即紀錄）
  - plan B（deploy＋cutover）：`2026-05-31-vm-deploy-and-cutover.md`（原文已不在 repo，本 ADR 即紀錄）
- 相關 ADR：[2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md)（reconciler 為唯一送單者）、[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)（現行部署模型）。
