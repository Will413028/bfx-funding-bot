---
title: 撤銷模擬 soak 與 N-1→N 升級測試這兩道發版閘門
date: 2026-11-08
status: active
tags: [bfx-funding-bot, decision, simulation, soak, testing, release]
---

# 撤銷模擬 soak 與 N-1→N 升級測試

## Context

[2026-10-03 ADR](2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md) D3 把模擬 venue 上的長時間 soak 定為閘門，之後的修訂把它改成 24 小時、加上故障注入與 kill／恢復，2026-11-06 再拆出獨立的 N-1→N 升級測試，發版要兩份報告都 PASS。

2026-11-04 起的 R4 發版前 soak 實際跑下來：

- 成本高：視窗重開了三次，每次至少再等 24 小時；VM 上要另外維護 `bfx_sim` database 與 role、`sim.env`、soak image workflow、報告腳本與它的 bootstrap 腳本。
- 抓到的大多是 harness 自己的問題（例如 bootstrap 腳本漏註冊外鍵目標表、runbook 換代步驟與門檻互相矛盾），不是 ledger、組裝或 runtime 的缺陷。

**約束**：

- `external` 單一使用者的 bot，在全部工作完成前沒有真錢（owner 2026-11-08）。沒有 soak 擋下的缺陷要付的代價只是一次修正與重新部署，不是資金損失。
- `external` 模擬 venue 不能驗證 Bitfinex 本身的語意（10-03 ADR D3 已寫明的代價），所以 soak 證明的範圍只到 ledger、組裝與 runtime，而這些已經有 CI 覆蓋。

## Options Considered

- **A. 撤銷兩道閘門，刪除只為它們存在的程式與設定**（選用）。
- **B. 保留 soak，只改成不擋發版的定期回歸。** 主機上的 sim 堆疊與報告腳本照樣要維護，而它抓到的大多是自己的問題。
- **C. 維持現狀。** 每次發版至少多 1.5 天，加上 harness 修補。

## Decision

- **撤銷**：24 小時模擬 soak 與 N-1→N 升級測試都不再是發版閘門。shadow-p14 的 Gate B（[2026-06-04 ADR](2026-06-04-adaptive-period-strategy-and-deploy-gating.md)）跟著撤回。
- **刪除**（同一個 PR）：`soak-image.yml`、`scripts/sim_soak_report.py`、`scripts/bootstrap_simulation_db.py`、`apps/sim_faults.py` 與 `BFX_SIM_FAULTS`、模擬 venue 的持久 `fault_injected`／`internal_failure_recorded` 事件、`VenueObserver` 與 `bfx_sim_venue_*` 計數器、讀即時行情的模擬 feed（`LiveMarketFeed`、venue 的 feed task 與 watermark lag 指標）、沒有其他使用者的公開 trades WS client 與公開 trades REST 讀取、`deploy/vm/shadow.env`、`deploy/vm/shadow-p14.env`、`docs/runbooks/simulation-soak.md`，以及 `bfx_sim` role 的文件。
- **simulated venue 只能由 `VenueSeam` 選上**：它只在測試組裝，feed、可選的 `FaultPlan` 與開場錢包（`VenueSeam.initial_wallets`，取代已刪除的 `BFX_SIM_INITIAL_WALLETS`）只能經 seam 注入，建好的 venue 經 `VenueSeam.on_built` 交給測試（`Daemon` 不再持有它）。單靠 env（`BFX_PHASE=shadow`）在 `load_config` 就拒絕，早於讀 DB 與送告警；沒有 seam 的 simulated 組裝以 `ConfigurationError` 拒絕；Bitfinex 組裝收到 seam 也拒絕。epoch 寫入者檢查兩個 venue 一樣（只收 genesis migration 與 S1-7 switch），simulated 不再豁免。
- **保留**：`simulated_venue` 核心、SQL store 與 `sim_venue_event` 表（schema 不動）、經 seam 注入的 `FaultPlan`、phase `shadow`（由後續 PR 另外決定）；realm `shadow` 已由 [2026-11-08-remove-the-shadow-realm](2026-11-08-remove-the-shadow-realm.md) 移除。unrouted request 只留在記憶體的 `unexpected` log 與計數，不再寫成持久事件。

剩下的保護：

- CI：單元與整合測試，含模擬 venue 的 oracle 與 daemon 測試。
- 部署前的備份與 restore test（`docs/runbooks/deploy.md`）。
- 開機時的 schema head 檢查。
- halt。

## Rationale

soak 要補的是「重啟、長時間漂移、部署互動」這段證據，而實際成本與產出不成比例：三次重開、多數發現屬於 harness。沒有真錢的期間，一個 soak 才抓得到的缺陷在 prod 出現時，修正並重新部署的成本低於每次發版先跑 soak。**不選 B**：不擋發版就沒有人讀它的報告，維護成本卻不變。**不選 C**：理由同上。

**代價**：多行程重啟、長時間漂移與 migration 套在既有資料上的行為，在部署前不再有模擬證據；只有部署時的 restore test 與上線後的觀測。持久的故障注入事件刪除後，已有這兩種事件的舊 venue log（只有 VM 上的 `bfx_sim`）讀不回來；那個 database 也一起退役。

## Revocation Triggers

- 開始放真錢，或有第二個使用者 → 重新評估發版前的長時間驗證。
- prod 出現 CI 抓不到、而 soak 本來抓得到的缺陷（重啟後狀態不一致、長時間漂移、升級 migration 在既有資料上失敗）→ 先補對應的整合測試；補不了才恢復對應的閘門。

## Related

- 被修訂：[2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue](2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md) D3 與它的 soak 修訂（見該檔 Amendment 2026-11-08）；[2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md) Gate B（見該檔 Amendment 2026-11-08）。
- 主機端（不在 repo）：見下節「主機殘留的 `bfx_sim`」。

## 主機殘留的 `bfx_sim`

本節是移除步驟的唯一來源，其他文件（`docs/runbooks/secret-rotation.md`、`docs/security/asvs-l2.md`、[2026-10-21 ADR](2026-10-21-single-vm-least-privilege-by-db-role-not-network-segmentation.md)）只連到這裡。

soak 的 runbook 已刪除，但 VM 上還留著它建的東西，直到 owner 移除：`bfx_sim` 登入 role（`bfx_bot` 的成員，只能連 `bfx_sim` database）、`bfx_sim` database，以及 `/opt/bfx/runtime/sim.env`（內含它的 `DATABASE_URL`）。它不是 migration 建的，repo 裡沒有程式再用它。

移除（`DROP` 不可逆，**要 owner 同意才做**；`<owner>` 見 `AGENTS.local.md`）：

1. 停掉 sim 的排程與容器：
   - `sudo systemctl disable --now bfx-sim-watch.timer`，再 `sudo systemctl stop bfx-sim-watch.service`（unit 不存在就略過）。
   - 一次性（transient，`systemd-run` 建的）unit：`systemctl list-units --all --plain --no-legend 'bfx-sim*'` 列出的每個 `<unit>` 執行 `sudo systemctl stop <unit>`，最後 `sudo systemctl reset-failed 'bfx-sim*'`；transient unit 停掉就消失，不需要 disable。
   - `docker rm -f bfx-sim`（已不在就略過）。
2. 刪掉密碼檔：`sudo rm /opt/bfx/runtime/sim.env`。
3. 收回 `bfx_sim` 擁有的物件與權限，**在它有權限的每個 database 各跑一次**（`bfx_sim`、`bfx`、`postgres`）：
   `docker exec bfx-postgres psql -U <owner> -d <db> -v ON_ERROR_STOP=1 -c 'REASSIGN OWNED BY bfx_sim TO <owner>' -c 'DROP OWNED BY bfx_sim'`。
   `bfx_sim` database 要在步驟 4 刪掉之前先跑；`DROP OWNED` 也會撤銷 grant 與 default privileges，否則 `DROP ROLE` 會因殘留依賴失敗。
4. 刪 database：`docker exec bfx-postgres psql -U <owner> -d postgres -c 'DROP DATABASE bfx_sim'`。
5. 刪 role：`docker exec bfx-postgres psql -U <owner> -d postgres -c 'DROP ROLE bfx_sim'`。
6. 確認（印 `0|0|0`）：
   `docker exec bfx-postgres psql -U <owner> -d postgres -Atc "SELECT (SELECT count(*) FROM pg_roles WHERE rolname = 'bfx_sim'), (SELECT count(*) FROM pg_database WHERE datname = 'bfx_sim'), (SELECT count(*) FROM pg_shdepend d JOIN pg_authid a ON a.oid = d.refobjid WHERE a.rolname = 'bfx_sim')"`；
   並確認 `systemctl list-units --all --plain --no-legend 'bfx-sim*'` 沒有輸出、`docker ps -a --filter name=bfx-sim -q` 沒有輸出。

舊的 `sim_venue_event` log 在這次變更後讀不回來（已刪除的事件型別無法重播），也不做 migration：唯一有這種列的是上面要刪掉的 `bfx_sim` database。
