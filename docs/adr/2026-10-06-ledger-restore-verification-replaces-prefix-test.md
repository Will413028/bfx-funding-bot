---
title: 例行還原測試改以 ledger 有界 digest 與唯讀 boot check 驗證，取代 prefix-hash 比對
date: 2026-10-06
status: active
tags: [bfx-funding-bot, decision, postgresql, disaster-recovery, ledger]
---

# 例行還原測試改以 ledger 驗證

## Context

prior state：每月與變更觸發的還原測試（[2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore](2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore.md) D5）
以還原副本的 `event_prefix_hashes` 鏈頭比對 production 同一 `event_seq` 的鏈結。2026-10-05 資本權威切換到 ledger
（journals + venue observations），event_log 凍結，這個比對只再驗證凍結資料。

**約束**：

- `external` 單機 VM、沒有 standby；restore test 以 `ubuntu` 執行，讀不到 runtime env，production 只能經 `docker exec bfx-postgres psql` 以 admin role 走 container socket 讀。
- `external` bfx-deploy 用目標 release 的 DR 腳本，但用已安裝（上一版）的 restore-test unit 與 wrapper，且 verifier image 是目前部署的 `bfx-bot:local`；新工具只在部署成功後安裝。
- `inherited` ledger 表 append-only（trigger），每個 scope 的寫入者在 commit 前都持有 scope advisory lock；但各表沒有共同的 commit stamp。

## Options Considered

- **基準：還原後以 append-only 資料在單調鍵上限內的 digest 與來源比對，加上 app 自己的啟動檢查**（推測：常見於 append-only ledger 的備份驗證；hash chain 系統則比鏈頭）。
- **A（採用，Will 2026-10-06 決策 D3 A+B）**：還原副本與 production 以同一份 bounded COPY 腳本讀 append-only ledger 表，比較每表 count 與 multiset sha256；可變表（clock、mirrors）只做還原副本的自洽與「clock 不超前 production」；另在 image 內對還原副本跑唯讀 boot check（schema head、realm、epoch=ledger、seed guard、capital reader 導出 basis），不連 venue。prefix verifier 同 PR 刪除。
- **W 從 production 取**：先讀 production 的 W 再還原，需強制 WAL switch 並等待 archive，否則還原副本可能不含 W；不採用。
- **每列共同 commit stamp（migration）**：最精確，但需動所有 ledger 表與寫入路徑；不在本次範圍。

## Rationale

- **W 取自還原副本**：與舊 prefix 做法同（還原副本的頭，再讀 production 同一點）；RPO 由 bfx-backup-check 負責。
- **各表有界鍵**：`query_revision`、`attempt_seq`、`opened_revision`、`epoch_seq` 在 scope lock 下分配；observation 家族（含 basis、quarantine member）以其 query 的 `query_revision`；resolution 只比對指向比還原副本最新 accepted observation 更舊者（嚴格 `<`）；outcome 排除還原時尚無 outcome 的 attempt。還原副本的有界集合必須等於整表（resolution 為至多），錯的邊界直接失敗而不是藏列。非 accepted observation 的提交順序依賴「每個 scope 一次一個 begin→accept 週期」；違反時是誤報（fail-closed），不是漏報。
- **兩端讀同一份 COPY 文字**而非 table_digest canonicalizer：table_digest 沒有以 W 為界的版本（對已部署 image 是新程式），且在 production 端跑 Python 需要新憑證。
- **wrapper 不帶模式**：wrapper 只呼叫 `--restore-test --output <path>` 並接受 `restore_test: true` 的新鮮 receipt，之後更換驗證方式不再需要跨版本橋接。本次仍保留過渡用 `--prefix`（上一版 wrapper 的呼叫），本 release 部署後移除。

## Result

- `deploy/vm/pgbackrest/ledger_digest.py`、`ledger_boot_check.py`、`restore_drill.py --restore-test`；runbook `docs/runbooks/offsite-dr.md`「Monthly and change-triggered ledger restore test」。

## Revocation Triggers

- 單次 production 讀取時間逼近 RTO 預算，或長 snapshot 影響 vacuum → 改為增量比對（上次驗證的 W 與 digest 存入 evidence，只讀 (W_prev, W]）或從 standby 讀。
- ledger 表新增共同 commit stamp → 以它作為唯一邊界。
- boot check 仍以 stdin 腳本依賴 image 內部 API；image 內建 check 入口後（本 release 部署之後即可）改呼叫該入口。
