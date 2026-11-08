---
title: 移除 realm `shadow`：資料庫只會是 `prod` 或 `ci`
date: 2026-11-08
status: active
tags: [bfx-funding-bot, decision, database, realm, simulation]
---

# 移除 realm `shadow`

## Context

realm（`deployment_environment`，資料隔離的範圍）原本有 `prod`、`shadow`、`ci` 三個值（[2026-05-23 realm ADR](2026-05-23-deployment-environment-realm-and-backend-ci-gate.md) D1）。[2026-10-03 ADR](2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md) D1 讓模擬 soak 跑在 realm `shadow` 的獨立 database（`bfx_sim`）。[2026-11-08 撤銷 soak 的 ADR](2026-11-08-retire-the-simulation-soak.md) 刪掉了 soak 與 `bfx_sim`，把 realm／phase 是否移除留給後續 PR。

現況（2026-11-08，`rg` 盤點）：沒有任何部署、腳本或測試組裝以 realm `shadow` 寫入；simulated venue 只在測試裡經 `VenueSeam` 組裝，測試資料庫一律 stamp `ci`；prod DB 的 stamp 是 `prod`，而且自 `a7c3e9f1b2d4` 起 trigger 拒絕與 stamp 不符的寫入。

**約束**：

- `external` prod DB 不會有 `shadow` 資料列（stamp migration 拒絕混合 realm 的 database，之後由 trigger 擋）。若真的有，migration 拒絕升級而不是猜測處理。
- `inherited` 每列的 `deployment_environment` 欄位與 realm trigger：仍有作用（擋 `prod`／`ci` 混寫），本 PR 不動；是否還需要每列標記另由 owner 決定。

## Options Considered

- **A. 移除 `shadow` 這個值，realm 只剩 `prod`、`ci`**（選用）。一個沒人寫的值留在列舉、CHECK 與 config guard 裡，只是讓每個讀者多想一種不存在的狀態。
- **B. 保留，等下一次需要長時間模擬再用。** 需要時再加回一個值的成本，比現在維護它低。

## Decision

- **D1**：realm 只有 `prod` 與 `ci`。`DeploymentEnvironment`、`KNOWN_REALMS`、`VALID_DEPLOYMENT_ENVIRONMENTS` 移除 `shadow`；`BFX_DEPLOYMENT_ENV=shadow` 在 `load_config` 以「must be one of prod, ci」拒絕。
- **D2**：simulated venue 只在 realm `ci`。`ALLOWED_REALMS` tuple 改為單一常數 `SIMULATED_REALM = "ci"`。三道關卡分工：`load_config` 的 `require_simulated_realm` 只允許 simulated phase 配 `ci`（允許清單，以後新增的 realm 在這裡放行前一律拒絕；讀 DB 前失敗）；`sim_venue_event.ck_sim_venue_event_realm` 為 `deployment_environment = 'ci'`（DB 的權威）；`SimAccount` 與 `SqlVenueEventStore.open` 比對常數（測試裡接錯 store 時拒絕）。
- **D3**：migration `96535191e443` 先檢查 `database_realm` 不是 `shadow`、每張受 realm 保護的表（掛著 `guard_database_realm` trigger 的表，任何 schema；從 `pg_trigger` 推導，不抄清單；partition 由 parent 涵蓋）沒有 `shadow` 列，沒有該 trigger 的表（例如 `release_archive` 的凍結副本）不是 live realm 資料、不擋升級。探測 SQL 在資料庫內以 `format()` 帶入名稱與引號，一個 statement 跑完；任一不符就拒絕並維持原 revision；再把 `ck_database_realm_realm` 收窄為 `realm IN ('prod', 'ci')`、`ck_sim_venue_event_realm` 收窄為 `= 'ci'`。downgrade 放寬回原本的 CHECK（放寬無損，[forward-only ADR](2026-10-08-forward-only-migrations.md) D1 允許）。
- **D4**：`load_config` 刪掉「live 拒絕 realm shadow」這條規則（值已不存在）；simulated phase 的規則改為允許清單：只允許 `ci`，其餘（含以後新增的 realm）一律拒絕。

## Rationale

- **D1／D2**：soak 退役後 `shadow` 沒有寫入者，留著它的唯一理由是「也許以後用得到」；不選 B，因為加回一個列舉值與一條 CHECK 的成本很低，而保留的成本是每條 realm 規則都要多處理一個不會出現的值。單一值用常數比較，不用 tuple 成員檢查，讀者不必猜還有哪些值。
- **D3**：「受 realm 保護」的定義就是掛著 realm trigger（`test_every_realm_column_has_the_trigger` 用同一個定義），所以新表只要接上 trigger 就會被檢查，不在任何 schema 清單上；沒 trigger 的表本來就不受 realm 約束，掃它只會讓凍結歸檔或暫存表擋住升級。
- **D2／D4**：允許清單而不是拒絕 `prod`：拒絕清單在新增 realm 時預設放行，允許清單預設拒絕，假成交寫進真錢資料的風險只能靠後者擋。CHECK 以 NOT VALID 加上再 VALIDATE，兩張表都很小，bfx-deploy 在 migration 期間停 webapi 與 bot。

## Followup

- **phase 改名 `shadow` → `simulated`**（下一個 PR）：phase `shadow` 只表示「經 seam 的 simulated venue」。改名時把 phase guard 簡化為「simulated 必須配 realm `ci`」，並清掉歷史上的 paper/shadow 註解。
- **移除 `ExecutionPolicy.OPTIMIZER_SHADOW`**（phase 改名之後）：沒有部署設定它，`live.env` 用 `book_guarded`。
- **每列 `deployment_environment` 欄位與 realm trigger**：本 PR 保留；資料庫只剩一個 realm 時是否還需要每列標記，交給 owner 另行決定。

## Revocation Triggers

- 需要在 `ci` 以外跑長時間模擬（例如恢復 soak）→ 重新評估是否加回一個模擬用 realm。

## Related

- 取代：[2026-05-23-deployment-environment-realm-and-backend-ci-gate](2026-05-23-deployment-environment-realm-and-backend-ci-gate.md) D1 的 `shadow` 值（見該檔 Amendment 2026-11-08）；[2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue](2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md) D1 的 realm `shadow`（見該檔 Amendment 2026-11-08）。
- 接續：[2026-11-08-retire-the-simulation-soak](2026-11-08-retire-the-simulation-soak.md)。
