---
title: pgBackRest 裝進 PostgreSQL image 作為備份 data-plane，還原演練採分段 egress 的隔離環境
date: 2026-09-04
status: active
tags: [bfx-funding-bot, decision, postgresql, disaster-recovery, reliability, security, deployment]
related-commits:
  - "497e8b4^..360d16d"
  - d75b288
---

# pgBackRest 裝進 PostgreSQL image，還原演練採分段 egress 隔離

## Context

prior state：production PostgreSQL 18 在單一 VM 的 `postgres:18-alpine` container，只有本地 daily `pg_dump`，
沒有 `archive_mode`、offsite WAL 或還原自動化。[2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md) 決定了 **R2 當 repository**；
本 ADR 記的是同一份 spec 裡的另一個選擇：**備份程式放哪裡、還原怎麼隔離**。provider 選擇見該 ADR。

**約束**：

- `external` 目標 RPO ≤300s、restore drill RTO ≤3600s；event-sourced DB 的還原必須能證明 event chain 與 empty-projector replay 一致，不是「dump 成功」。
- `external` R2 不支援 S3 Object Lock headers；Bucket Lock 可能讓 pgBackRest `expire` 永久失敗，所以不能以 WORM 為前提。
- `external` 單一 operator、單一 VM；還原演練不得碰 production volume `bfx_pgdata`、不得連 Bitfinex、不得取得 app secrets。
- `inherited` event log 是 SoT、projection 可由 log 重建（[2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md)）：仍成立，是還原驗證的判準。

## Options Considered

- **基準. PostgreSQL continuous archiving＋PITR，由 DB 主機上的專用工具執行**：PostgreSQL 官方把 WAL archive＋base backup 列為 PITR 的做法，pgBackRest user guide 也假設裝在 DB 主機（[Continuous Archiving and PITR](https://www.postgresql.org/docs/18/continuous-archiving.html)、[pgBackRest User Guide](https://pgbackrest.org/user-guide.html)）。容器化時對應到 A。
- **A. 自建 PostgreSQL image 內含 pinned pgBackRest（採用）**：`archive_command`、backup、restore 共用同一個 binary／config；secret 以唯讀 `conf.d` fragment 掛入。代價是 DB image 要自己 build，改動要在維護窗口 recreate PostgreSQL。
- **B. host 安裝 pgBackRest，經 SSH/TLS 遠端操作 container 內 PG**：不動 DB image；代價是多一層 host↔container protocol、權限、憑證與第二條 restore path。
- **C. 應用層定期 `pg_dump` 上傳 R2**：最快；沒有 continuous WAL/PITR，達不到 RPO ≤5m，也容易把「dump 成功」誤當成還原驗證。

還原隔離的子選項（2026-09-05 hardening 修訂）：

- **S1. 單一 internal network，verifier 與 restore-db 同網**：初版做法；但 restore-db 還原時必須連 R2，internal-only 網路讓 restore 本身跑不動。
- **S2. 分段 egress（採用）**：restore-db 暫時同時掛 generated internal＋R2 egress network，確認 `pg_is_in_recovery()=false` 後斷開 egress，verifier 只在 internal network、以臨時最小權限 role 驗證。

## Decision

- **D1 = spec §Options A**：採 A。pgBackRest 2.59.1 從官方 tarball 以 SHA-256 核對後編進 image，base image 以 digest pin；只寫小型 wrapper 處理 preflight、排程、redacted evidence 與演練，不自寫備份格式、retry 或 retention。
- **D2 = spec §pgBackRest configuration**：`archive_mode=on`＋`archive_timeout=60s`（低寫入量也有上限的 archive 延遲）＋`archive-async`；每週日 03:17 UTC full、其餘日 diff，保留 4 組 full × 各 6 組 diff；repository client-side `aes-256-cbc`，cipher passphrase 與 R2 key 分開保存並離線 escrow；pgBackRest 的 file/console/stderr log 全關，只留 allowlisted error code。
- **D3 = spec §Scheduling**：R2／archive 失敗只經 status 與告警呈現，**不接 autoheal**，避免反覆重啟資料庫；排程用 `docker exec --user postgres`，不用繼承 app healthcheck 的 `compose run`。
- **D4 = hardening revision 1–6**：採 S2。還原後的非空 PGDATA 不靠 `POSTGRES_*` 初始化，改以 local socket、SQL role `bfx` 建臨時 verifier role；每次 measured 還原都要同一 backup／PITR target 的 baseline（migration heads、event count/head/hash、account/environment/projector）逐欄吻合；整個生命週期共用 3600 秒單調 deadline，cleanup 另有 30 秒；任何失敗以 `measured:false` 原子取代舊的綠燈（含 ENOSPC）。
- **D5 = 後續（2026-09-25，`d75b288`）**：每月與變更觸發的還原測試改用 `event_prefix_hashes` 前綴比對，不需 baseline、不必停寫入者；baseline 模式留給完整演練。（2026-10-06 由 [2026-10-06-ledger-restore-verification-replaces-prefix-test](2026-10-06-ledger-restore-verification-replaces-prefix-test.md) 取代：資本權威改為 ledger 後 event_log 凍結，前綴比對只驗證凍結資料。）

## Rationale

- **D1 選 A 而非 B**：單一 VM 上 B 多出的 protocol、憑證與第二條 restore path 沒有對應收益；A 讓 archive、backup、restore 三條路徑同一份 binary，也不必把 Docker socket 交給 sidecar。代價：DB image 由我們維護，升級 PostgreSQL 或 pgBackRest 要重 build 並驗 digest。
- **D1 不選 C**：C 沒有 PITR，RPO 由 dump 間隔決定；而且 event-sourced 系統的可還原性要看 event chain 與 replay，dump 成功不代表這件事。
- **D2**：`archive_timeout` 是 RPO 的上界控制而非效能設定；retention 只由 pgBackRest 一個 writer 管（R2 lifecycle 不刪物件，見 [2026-09-05-offsite-dr-terraform-runtime-ownership](2026-09-05-offsite-dr-terraform-runtime-ownership.md)）。代價是保留期間物件會累積，需量測容量後再調。
- **D4 選 S2 而非 S1**：S1 在 restore 階段就被自己的隔離擋住；S2 讓連外只存在於「下載 WAL／backup」那一段，驗證階段完全沒有對外網路與 R2 憑證。代價是 runner 要管理兩個 generated network 與斷線順序，並逐步驗證 membership。
- **D4 的 baseline**：相較只比對「restore 後的 DB 自己算出的 hash」，外部 baseline 才能抓到還原到錯的 target；代價是每次完整演練要先捕捉 baseline。D5 以 prefix hash 取代例行測試的 baseline，因為前綴鏈本身就是 production 的可比對真相。

## Result

- `git log --oneline 497e8b4^..360d16d -- deploy/vm docker-compose.bot.yml docker-compose.dr.yml`（PR #16）＋ `d75b288`（prefix 還原測試）。
- 2026-09-12 production：R2 smoke、fresh backup／preflight RPO=`1s`、isolated restore event-chain 驗證通過；full restore RTO `130–205s`（量測與調校過程見 [2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md) 的 Amendment）。
- 現行權威：repo `backend/ARCHITECTURE.md` §7「Offsite DR source of truth」與 `docs/runbooks/offsite-dr.md`（含 monthly prefix restore test）。

## Revocation Triggers

- 改用第二台 DB 主機或 managed PostgreSQL → B／託管備份重評。
- pgBackRest 對 PostgreSQL 新主版本停止支援，或 R2 相容性讓 archive-async 不穩定。
- 需要原生 WORM（外部資金、合規）→ 與 R2 ADR 一併重評。

## Related

- 來源（Provenance）：
  - spec：`2026-09-04-offsite-dr-cloudflare-r2-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - plan：`2026-09-04-offsite-dr-foundation.md`（原文已不在 repo，本 ADR 即紀錄）
  - plan：`2026-09-05-offsite-dr-hardening.md`（原文已不在 repo，本 ADR 即紀錄）
- provider 選擇與 RTO gate 歷史：[2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md)；Terraform／secret ownership：[2026-09-05-offsite-dr-terraform-runtime-ownership](2026-09-05-offsite-dr-terraform-runtime-ownership.md)
- DR 與發版脫鉤（每月＋事件觸發還原測試）：[2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md) D6，由 [2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) 沿用
