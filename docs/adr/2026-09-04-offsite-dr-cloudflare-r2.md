---
title: Offsite DR 使用 Cloudflare R2 + pgBackRest
date: 2026-09-04
status: active
tags: [bfx-funding-bot, decision, architecture, postgresql, reliability, security, deployment]
---

# Offsite DR 使用 Cloudflare R2 + pgBackRest

## Context

目前 production PostgreSQL 位於 OCI VM；現有備份是 VM 本地 daily `pg_dump`，尚無 continuous offsite WAL/PITR 或 monthly isolated restore automation。外部 beta 前必須驗證 RPO ≤5m、restore drill RTO ≤60m、event chain 與 event-only projection rebuild。

Halt 2 runbook 另有更嚴格的 pre-cutover restore evidence gate；本 ADR 初版保留該 gate。
2026-09-12 amendment 改採既有 operational target，R2 選擇本身不變。

## Options Considered

- **A. Cloudflare R2（選用）**：S3-compatible、免 egress charge、可使用 bucket-scoped
  token 與 lifecycle；代價是 Object Lock 相容性不足，需 client-side encryption、
  access restriction 與額外保留策略補償。
- **B. Backblaze B2**：S3-compatible、成本與 immutable-storage 選項具吸引力；
  代價是新增另一個 provider/account 與維運邊界。
- **C. AWS S3**：相容性、Object Lock 與工具支援最完整；代價是成本、IAM 與
  solo-operator 維運複雜度較高。
- **D. OCI Object Storage**：與 VM 同 provider、網路整合較直接；代價是 provider
  failure domain 重疊，不符合較強的異地隔離目標。

## Decision

採用 Cloudflare R2 作為 offsite repository，先使用 private Standard bucket、S3 endpoint `https://<account-id>.r2.cloudflarestorage.com` 與 `region=auto`。PostgreSQL backup/archive layer 採 pgBackRest，包含 WAL archive、base backup、client-side AES-256 repository encryption 與 lifecycle retention。

R2 credentials 只放 VM secret/config boundary，不進 repository；token 優先限制至單一 bucket 與必要的 read/write scope。R2 Object Lock 不可用或不相容的部分列為 residual risk，外部 beta 前若無法以 compensating controls 接受，改評估第二個 immutable provider。

## Rationale

R2 在現有 operational preference 下能保留標準 S3 工具鏈與低摩擦 restore egress，同時避免把 backup 與 OCI VM 綁在同一 provider。相較 B2／AWS S3，這犧牲了部分 WORM/immutability 能力；此 trade-off 只有在 encryption、least privilege、retention 與 restore drill 可量測成立時才接受。

## Expected Outcome

持續 WAL/PITR 與週期性 base backup 可在 isolated PostgreSQL restore；restore 後
驗證 row counts、event head/hash、event-only projection replay 與 account scope。
Operational DR 目標為 RPO ≤300s、restore drill RTO ≤3600s；2026-09-12 起 Halt 2
亦採用 `restore_rto_seconds ≤3600s`，其餘 evidence gates 不變。

## Followup

- 依 [2026-09-05-offsite-dr-terraform-runtime-ownership](2026-09-05-offsite-dr-terraform-runtime-ownership.md) 以 Terraform 建立 private Standard bucket 與 conservative incomplete-multipart lifecycle；由 operator／VM runtime 建立 scoped token、pgBackRest secret 與 timer activation。
- 建立 pgBackRest/PostgreSQL archive config、status/alert 與 retention schedule。
- 建立不連 Bitfinex 的 isolated restore drill，接上 `verify_projection_replay.py`。
- 量測 RPO/RTO 並將 backup、restore、event hash evidence 寫入 runbook。
- 若 R2 compatibility smoke test 或 deletion-risk review 不通過，評估第二個
  immutable provider。

## Revocation Triggers

- R2 S3 compatibility 無法穩定支援 pgBackRest。
- 實測 RPO >5m 或 restore drill RTO >60m。
- token、retention 或 deletion risk 無法以 compensating controls 接受。
- isolated restore 無法重建一致 event chain 或 projection。
- 外部 beta 的合規／不可否認性要求必須使用原生 WORM。

## Review Notes

2026-09-04：使用者明確選擇 Cloudflare R2。
2026-09-05：implementation/hardening 已在 local `codex/offsite-dr-foundation` 累積 40 commits，並合併 PR [#16](https://github.com/Will413028/bfx-funding-bot/pull/16)（merge `360d16d9`），包含 pgBackRest archive、R2 Terraform、secret-safe bootstrap、fail-closed timers、isolated restore、evidence freshness 與 operator review；production archive settings、R2 runtime token、VM secret、timers 與 restore drill 尚未部署或驗收，決策仍 active。runtime ownership 另見 [2026-09-05-offsite-dr-terraform-runtime-ownership](2026-09-05-offsite-dr-terraform-runtime-ownership.md)。
2026-09-12：production archive／R2 runtime boundary 已接上；R2 smoke、fresh backup/preflight（RPO=`1s`）與 isolated restore event-chain verification 通過。full restore measured RTO=`202s`，未達 Halt 2 的 `≤60s` hard gate，因此 R2 decision active 但 production canary／resume acceptance 未解鎖。

### Amendment Decision（2026-09-12 B1 benchmark）

Task 1 以 `14c1757` 加入 bounded restore stage timing；Task 2 在 production VM 以相同 backup、baseline、image、config 與 verifier 完成三次 valid isolated cold restore，RTO 為 `174/176/178s`（median=`176s`）。之後明確核准單一變數 B1：只在 isolated checkout 加入 pgBackRest `buffer-size=4MiB`；三次 candidate RTO 為 `174/179/177s`（median=`177s`）。兩輪 fresh preflight 都是 RPO=`1s`、failed archive=`0`，event chain、archive/active parity、full verifier、cleanup 與 network isolation 通過。

**Options Considered**

- **A. 保留 production halted，拒絕 B1 作為解鎖條件**：維持 correctness 與既有 recovery safety gate；代價是交易暫不能恢復。
- **B. 接受 B1 並解鎖 Halt 2**：可立即恢復交易，但 RTO median 仍遠高於 `≤60s`。
- **C. 另核准新的 candidate 或硬體方案**：可能改善 RTO，但需重新做 3 baseline + 3 candidate 與完整 correctness gate。

**Decision**

採用 A。B1 未改善 RTO，不解鎖 Halt 2，不啟動 bot，不恢復交易，也不放寬 `≤60s` gate。

**Rationale**

B1 的 RTO median=`177s`，較 baseline median=`176s` 反而增加 `1s`；isolation／cleanup 略快，但 physical/WAL 與 verification 沒有改善。相較接受 B1，維持 `≤60s` 才符合既有 safety contract；相較直接進入 C，下一個 candidate 必須先取得明確核准，避免 uncontrolled tuning。aggregate CPU 約 30%，也不足以單獨證明 CPU saturation；既有 `repo1-block`、`repo1-bundle`、`archive-async` 與先前 `process-max=4`／archive-mode 比較不構成通過 gate 的理由。

**Expected Outcome**

保留目前已驗證的 R2、archive、RPO 與 isolated restore correctness；不把 partial green 誤報為交易可恢復。production halt、app services、canary permit 與 timers 維持停止。

**Followup**

沿用既有 Offsite DR Pending：另行核准下一個 supported reversible candidate 或硬體方案後，執行同 scope 的 3 baseline + 3 candidate，並重新確認 RPO、archive/active parity、full verifier、cleanup 與 RTO。B1 private evidence 只作診斷紀錄，不是 deploy 或 resume permit。官方 pgBackRest 文件只證明設定受支援，不代表可通過 Halt 2 gate：[User Guide](https://pgbackrest.org/user-guide.html)、[Configuration Reference](https://pgbackrest.org/configuration.html)。

### Amendment Decision（2026-09-12 B2 process-max=8 benchmark）

Will 核准只在 isolated candidate 測試 pgBackRest `process-max=8`。初版 `3417320` 將選項放入 production／DR 共用 config，經 independent review 判定不符合隔離要求；該版本未推送並已 amend。修正版 `2f2e8d7` 保留 production baseline，新增 DR-only tracked config 與 explicit `--pgbackrest-config` wiring 後重新測量。

**Options Considered**

- **A. 保留 production halted，拒絕 B2 作為解鎖條件**：維持既有 correctness 與 `≤60s` safety gate；代價是交易暫不能恢復。
- **B. 接受 B2 並解鎖 Halt 2**：B2 median=`131s`，仍高於 `≤60s`，會把 partial improvement 誤當成 recovery readiness。
- **C. 另核准下一個 supported candidate 或硬體方案**：可繼續找 physical/WAL bottleneck 的改善，但需重新取得明確核准並做完整 3-run evidence。

**Decision**：採用 A。B2 修正版三次 isolated cold restore 為 `133/131/130s`（median=`131s`）；archive/parity/replay、RPO、network isolation、cleanup 與 fresh preflight 全通過，但不解鎖 Halt 2、不啟動 bot、不恢復交易，也不把 candidate merge 到 production `main`。

**Rationale**

B2 相較 Candidate A median=`137s` 少 `6s`，但 physical/WAL stage 仍約 `96.5–100.3s`，距離 `≤60s` 尚有明顯差距；相較接受 B2，維持 halted 才符合既有 safety contract。修正版把 `process-max=8` 留在 DR-only config，代價是增加 explicit override 與 source-separation tests，但避免 shared production config、backup timer 與 isolated restore 共用 candidate tuning。

**Expected Outcome**：保存 B2 的 isolated diagnostic evidence 與 config-isolation guard；production baseline、image tag、app services、timers、R2 archive 與交易 halt 維持原狀。B2 結果只支持「候選略有改善」，不支持恢復交易。

**Followup**

若要繼續縮短 RTO，需另行核准 supported software candidate 或硬體方案；不得以 B2 的 `131s` median 取代 `≤60s` gate，也不得進行未核准的 tuning sweep。

## Updates (2026-09-12)

- Historical evidence retained: `a9c7564` 三次 RTO=`133/137/137s`（median=`137s`）；B3 `c7a42e3` 三次 RTO=`133/131/137s`（median=`133s`），兩者 correctness／RPO=`1s`／network isolation／cleanup 全通過；以下 amendment 僅取代當時的 `≤60s` policy conclusion。（[a9c7564](https://github.com/Will413028/bfx-funding-bot/commit/a9c7564)；[c7a42e3](https://github.com/Will413028/bfx-funding-bot/commit/c7a42e3)）

### Amendment Decision（2026-09-12 Halt 2 RTO operational target）

B3 的 measured restore 只有原 `≤60s` 時間 gate 未過；event-chain、projection parity、RPO、network isolation、cleanup 與 verifier 均通過。Will 選擇在單一 operator／production account 情境優先恢復交易。

**Options Considered**：**A. 採用既有 operational target `≤3600s`（選用）**，放寬時間但保留所有 identity、schema/image/config、snapshot、replay、uncertainty、persistent-halt、permit、canary 與 reconcile gates；**B. 維持 `≤60s`**，安全餘裕最大但繼續 halted；**C. 移除 RTO hard gate**，最快但把未量測的 restore outage 上限交給實際故障，不能接受。

**Decision**：採用 A。`HALT2_MAX_RESTORE_RTO_SECONDS` 固定為 `3600`；RPO `≤300s`、event/projection parity、account identity、fresh full-account snapshot、open uncertainty、persistent halt、bounded permit/canary 與兩次 reconcile gates 全部不變。這只解鎖 RTO 時間 gate，不是 resume permit。

**Rationale**：相較 B，A 接受較長 restore outage，換取在 correctness、RPO 與 isolated-restore evidence 可驗證時盡快恢復單一使用者交易，代價是故障時最多容忍 60 分鐘 restore；相較 C，A 保留可量測上限與 fail-closed gates，不把「能還原」誤當成「可安全交易」。

**Expected Outcome**：既有 133–137s measured restore 通過時間 gate；仍須用已部署 policy 重新產生 fresh evidence、full-account snapshot/reconcile、replay/conversion/quarantine、單一 bounded canary 與兩次 ordered reconcile，全部通過後才可由 operator separately authorize resume。

**Followup**：先 push `origin/main` 再由 operator deploy；依 Halt 2 runbook 完成 fresh evidence、one-shot permit、canary 與 post-canary gates。任何 mismatch、UNKNOWN、orphan、exposure diff、stale snapshot 或 RTO >3600s 都維持 halted。

**Revocation Triggers**：measured restore 不可得、RTO >3600s、event/projection parity 失敗、RPO >300s、identity/schema/image/config mismatch、open uncertainty、snapshot 不完整，或 canary/reconcile 任一 hard gate 失敗。

## Related

- 備份 data-plane（pgBackRest 進 PG image、分段 egress 還原、retention）另見 [2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore](2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore.md)；前身 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)。
- 來源（Provenance）：spec `2026-09-04-offsite-dr-cloudflare-r2-design.md`；plans `2026-08-31-halt2-canary-runbook.md`、`2026-09-10-dr-rto-measurement.md`、`2026-09-12-halt2-rto-operational-target.md`、`2026-09-05-offsite-dr-hardening.md`（原文已不在 repo，本 ADR 即紀錄）。
- Commit/source retrieval: `deploy/vm/pgbackrest/restore_timing.py`（舊 commit hash 已隨 repo 公開（2026-09-27）改寫歷史而失效）；RTO gate 常數 `HALT2_MAX_RESTORE_RTO_SECONDS` 的落地與退場見 repo `git log --oneline -S HALT2_MAX_RESTORE_RTO_SECONDS`。
- Operator evidence：production VM 上的 RTO task2 與 candidate B1 證據檔（私有、非 repo）。
- [PostgreSQL Backup and Restore](https://www.postgresql.org/docs/18/backup.html)；[pgBackRest User Guide](https://pgbackrest.org/user-guide.html)；[Cloudflare R2 S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/)；[R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/)；[R2 object lifecycles](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)
- 2026-09-25 採用 [2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md)：以變更分級＋執行期限額取代每個 build 的人工 ceremony，DR 與發版脫鉤。
