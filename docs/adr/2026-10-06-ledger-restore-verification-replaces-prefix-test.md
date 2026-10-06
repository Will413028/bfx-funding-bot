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
- **wrapper 不帶模式**：wrapper 只呼叫 `--restore-test --output <path>` 並接受 `restore_test: true` 的新鮮 receipt，之後更換驗證方式不再需要跨版本橋接。
- **雙向過渡**：正向，drill 仍接受上一版 wrapper 的 `--prefix ...` 呼叫並寫 `restore-prefix.json`（`kind: restore_prefix`）。反向，revert 到 D3 之前的 release 時，新 wrapper 以 `--help` 探測目標 drill 是否有 `--restore-test`（不靠猜測實跑的 exit code），沒有就以舊形式呼叫（`/home/ubuntu/bfx/restore-test.json` 的 scope）並照舊 wrapper 的條件接受舊 receipt。兩個方向與 `restore-test.json` 都在 PR-D（docs／cleanup）移除。
- **工具安裝中斷的視窗**：bfx-deploy 部署成功後才逐檔安裝新 wrapper 與 unit。若停在「新 wrapper、舊 unit」之間，新 wrapper 接受舊 unit 的完整參數（`--config` 是 `--legacy-config` 的別名，`--evidence .../restore-prefix.json` 只是輸出路徑）。反方向「舊 wrapper、新 unit」（舊 wrapper 不認得 `--legacy-config`／`--legacy-evidence`）從這邊修不了：restore test 會 fail-closed（告警、擋下需要 restore test 的部署，不碰交易狀態）；重跑 tooling 安裝並 `systemctl daemon-reload`，或任一不觸發 restore test 的成功部署（會重新安裝工具），即可恢復。
- **讀取失敗的原因**：psql 的 stderr 只保留最後 2 KB 用來分類成固定的 cause（`statement_timeout`、`transaction_timeout`、`lock_timeout`、`idle_in_transaction_timeout`、`permission`、`connection`、`client_deadline`、`other`），寫進 receipt 的 `cause` 與 journal；原文不落地，因為錯誤的 DETAIL 可能引用列值（psql 的 argv 不含 DSN 或密碼：admin role 走 container socket）。
- **信賴 physical restore 的交易一致性**：W 與 scope 清單都取自還原副本，所以「還原副本缺了某個以自身為界的表的尾端，或缺了整個 scope」在 production 端看不出來。這依賴 PostgreSQL physical restore（base backup＋WAL replay 到一致點）本身就是交易一致的快照。沒有加跨 scope 的時間比對：scope 之間沒有 commit 順序，只能比 `started_at_ms`，restore point 落在新 scope 第一次 query 的 begin 與 commit 之間時會誤報。
- **boot check 比 bot 嚴**：boot check 要求資料庫裡每個 scope 都通過；bot 開機只檢查它自己的一個 scope。現在 prod 只有一個 scope，兩者等價。
- **production 讀取有界**：讀取 session 設 `statement_timeout` 與 `transaction_timeout`（drill 剩餘預算減 1 秒）、`idle_in_transaction_session_timeout` 60 秒、`lock_timeout` 10 秒；client 被殺時 server 端的 snapshot 與 AccessShareLock 最多留到預算用完，與月測重疊的 migration 最多等這麼久。receipt 的 `ledger.read_seconds` 與 heartbeat 記錄兩端讀取秒數，讓成長看得見。
- **verifier role 最小權限**：只 GRANT `ledger_digest.VERIFIER_TABLES`（ledger 表＋`alembic_version`、`database_realm`、`capital_policy_heads`、`capital_policy_revisions`），清單由 RULES 推導並有測試釘住。

## Result

- `deploy/vm/pgbackrest/ledger_digest.py`、`ledger_boot_check.py`、`restore_drill.py --restore-test`；runbook `docs/runbooks/offsite-dr.md`「Monthly and change-triggered ledger restore test」。

## Revocation Triggers

- ~~PR-D（D3 的 docs／cleanup PR）→ 移除 drill 的 `--prefix` 過渡呼叫、wrapper 的反向 fallback 與 `--legacy-*` 參數，並刪除 VM 上的 `/home/ubuntu/bfx/restore-test.json`~~：已在 S1-8 PR-D 執行，見下方 Amendment（VM 上的檔案由 operator 刪除）。
- ~~D4b（legacy 表移到 archive schema）→ 完整 baseline 演練（Halt 2、事故驗收）也改驗 ledger~~：已在 D4b 執行，見下方 Amendment。
- 改用 logical 或部分還原（不再是 physical restore）→ 重評「W 與 scope 清單取自還原副本」，加上 production 端的 scope／尾端完整性比對。
- 出現第二個交易所帳戶或 scope → 重評 boot check 要求每個 scope 都通過（bot 只看自己的 scope）。

- 單次 production 讀取時間逼近 RTO 預算，或長 snapshot 影響 vacuum → 改為增量比對（上次驗證的 W 與 digest 存入 evidence，只讀 (W_prev, W]）或從 standby 讀。
- ledger 表新增共同 commit stamp → 以它作為唯一邊界。
- ~~S1-8 PR-C 起 image 內建入口 `python -m bfx_funding_bot.apps.restore_boot_check`；drill 的 `ledger_boot_check.py` 先以 `find_spec` 探測，有入口就交給它，沒有（PR-C 之前的 image，只在部署 PR-C 那一次）才跑 stdin 腳本自帶的舊 API 版檢查。**PR-C 部署後的第一個 release** → 刪除該 fallback，drill 直接對 image 跑 `python -m`。~~：已在 S1-8 PR-D 執行，見下方 Amendment。

## Amendment 2026-10-06（S1-8 D4b）

完整 baseline 演練改驗 ledger，legacy 部分刪除。prior state：operator 在停寫時擷取 `baseline.json`
（event count／head／hash），drill 還原指定 backup 後跑 `verify_projection_replay`／
`verify_projection_archive` 比對凍結的 legacy event chain。D4b 把 12 張 legacy 表移到 `legacy_archive`
並撤銷權限後，這條路只會再驗一次凍結資料。

- **基準**同上：append-only 資料以單調鍵為界、跟來源比對，加上 app 自己的啟動檢查。上面的 A 已經是
  這個做法，差別只在 backup 與 recovery target 是誰選的。
- **採用**：驗收演練成為 ledger 模式的一個參數：`restore_drill.py --backup-label L [--target-time T]`。
  W 仍取自還原副本，production 在 W 以內的 append-only 列就是 baseline，所以不再需要 operator 擷取
  baseline 檔，也不需要停寫。receipt 寫到 `restore.json`（`kind: restore_ledger`、`restore_test: false`、
  帶 target）。`--restore-test` 與 wrapper 的契約不變。
- **刪除**：`DrillRequest`、baseline 載入、legacy receipt（`restore`／`archive_restore`）、
  `archive_evidence.py`、compose 的 `verifier` service、`scripts/verify_projection_replay.py`、
  `verify_projection_archive.py`，以及只有它們（和已完成的 projection／identity cutover）用到的
  `boot_recovery`、`event_store.replay_verification`、`projection_cutover/*`（`tables.py` 保留給
  alembic 比對）、`cutover_projection`、`cutover_identity`／`identity_cutover`。
- **限制**：production 必須還在而且讀得到。production 已經不在時要做的是事故還原，不是這個演練；
  還原副本必須在部署中 release 的 schema head，比 head 舊的 backup 會被 boot check 拒絕。

## Amendment 2026-10-06（S1-8 PR-D）

上面兩個 PR-D 觸發條件執行。prior state：drill 接受上一版 wrapper 的 `--prefix ...` 呼叫並寫
`restore-prefix.json`；wrapper 以 `--help` 探測 drill、對 D3 之前的 drill 走 `--prefix` 並讀
`/home/ubuntu/bfx/restore-test.json`；`--legacy-config`／`--config`／`--legacy-evidence` 讓安裝中斷時
新 wrapper 仍接受舊 unit；drill 把 `ledger_boot_check.py` 經 stdin 餵給 image，image 沒有入口時跑舊 API 版。

- **刪除**：drill 的 `--prefix`（含 `--account-id`／`--environment`／`--projector-version`）、
  `restore_prefix` receipt kind、`--rehearsal`（comparison rehearsal，隨切換工具刪除）；wrapper 的
  `--help` 探測與反向 fallback、heartbeat 的 `legacy_drill`（`--legacy-*`／`--config` 只剩接受後忽略，
  見下）；`deploy/vm/pgbackrest/ledger_boot_check.py`。drill 直接在部署中的 image 跑
  `python -m bfx_funding_bot.apps.restore_boot_check`（無 `-i`、不 pipe）。
- **跨版本**：PR-D 本身的部署 gate 是 d44fc7ab 的 wrapper／unit 跑 PR-D 的 drill（wrapper 探測到
  `--restore-test`，走無 mode 的呼叫；image 有入口）；revert 到 D3 之後任一版本時，PR-D 的
  wrapper／unit 只呼叫 `--restore-test --output`，那些 drill 都接受。`tests/scripts/
  test_dr_restore_test_cross_version.py` 以 d44fc7ab wrapper 的原檔驗證兩個方向（PR-D 部署後刪除）。
  revert 到 D3 之前的版本不再支援。
- **安裝中斷的視窗不再存在**（取代上面「工具安裝中斷的視窗」）：視窗來自 unit 傳了 wrapper 的
  參數。unit 現在只傳 `--drill`，receipt 與 heartbeat 路徑由 wrapper 自己的預設決定（與舊 unit 傳的值相同）；
  `--evidence`／`--heartbeat` 仍是可選的覆寫，舊 unit 的 `--legacy-*`／`--config` 接受後忽略。所以
  「新 wrapper、舊 unit」與「舊（d44fc7ab）wrapper、新 unit」都照常通過（`test_dr_restore_test_cross_version`），
  之後換 wrapper 參數也不必再經過 unit。
- **VM**：`/home/ubuntu/bfx/restore-test.json` 與 `restore-prefix.json` 不再被讀取，operator 可刪除。
