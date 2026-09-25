# Projection audit cutover release handoff

> **歷史紀錄。** 這次切換已完成。文中的 canary gate、Halt 2、手動 image／migration 步驟屬於已刪除的
> 舊 release 流程；現行部署見 [deploy runbook](deploy.md)，停機與恢復見
> [operations runbook](operations.md)。

本程序交付 release evidence 與 operator approval package，不授權 production
schema／grants／archive／apply、merge、push 或 resume trading。所有範例變數均須
由核准的 scope 與獨立證據填入；本文件不包含 production identity 或實測數字。
Synthetic pytest、舊 private capture 都不能冒充 fresh production snapshot。

## Approval 與隔離前提

開始 production 操作前，operator 必須核准 exact account UUID、environment、DB
target、image/config/projector、migration heads、backup label/PITR target、event
count/head/hash、classified differences、rollback evidence 與維護時窗。
私有 rehearsal 必須另有資料存取授權，使用 isolated restore，禁止 venue writes。
不要因目前可用的 company tailnet 自動切換機器全域 Tailscale profile。

- Schema 套用只能在核准目標的 `backend/` 執行 `uv run alembic upgrade head`。
  Archive migration 不代表 runtime role 已安全；不得順帶更改 production grants。
- 實際 bot、webapi、frontend、weekly-report roles 必須逐一證明 archive
  INSERT/UPDATE/DELETE/TRUNCATE、column grants、ownership、schema CREATE 均不可達。
  包含 inherited／SET ROLE 路徑、PUBLIC 與 default grants；superuser、CREATEROLE、
  CREATEDB、REPLICATION、BYPASSRLS 都是 blocker。用無權限的 synthetic role 測試
  通過，不能證明真實 runtime role 已符合要求。
- Persistent halt 不等於 quiescence。停止 bot/webapi/frontend/autoheal 與 one-off
  migrate/weekly-report writers，restart policy 必須為 `no`；mask 並停止
  `bfx-weekly-report.{service,timer}`、`bfx-halt-watch.{service,timer}` 與適用的
  `bfx-l3-verify-24h.service`。檢查 sockets、paths、transient units、未知 recreate
  sources、DB runtime sessions（含 idle）及其他 client transactions。
  pgBackRest backup/status units 不需因此停用；維持 RPO。
- `operations` 是 private codec file：`project=bfx`、實際 local `daemon_id`、
  `images` 完整包含 bot/webapi/frontend/autoheal/migrate/weekly-report/postgres/redis，
  `runtime_roles` 完整包含 bot/webapi/frontend/weekly-report。Image 值是 immutable
  local `sha256:…` identity；bot 必須等於 `--image-digest`。
  Probe 固定使用 Linux `/usr/bin/docker --host unix:///var/run/docker.sock` 與 systemd，
  拒絕 remote Docker context。盤點不會阻止另一個 administrator 日後啟動 writer。

## Local evidence 與 bounded cleanup

`diagnose` 與 classification 都是 **v2 directory**，不是單一 JSON：

```text
diagnostic/                 classification/
  manifest                    manifest
  COMPLETE                    COMPLETE
  chunks/                     chunks/
    <part files>                <part files>
```

Directories 必須 mode `0700`、regular files `0600`，拒絕 symlink、額外檔案、缺件、
錯序、digest 不符與未完成目錄。`COMPLETE` 最後寫入；失敗留下的 partial directory
不可續寫或視為成功。固定上限：record 1 MiB、part 4 MiB、artifact 2 GiB、4096 parts、
manifest 16 MiB。這是各 artifact 的格式限制，不是整次 rehearsal 的總 disk quota；
需另估兩個 artifacts、archive spool、dump/restore 與備份的空間。

`--diagnostic-digest`／`--classification-digest` 是 EvidenceManifest 的 semantic digest，
**不是** `sha256sum manifest`。其他 `*-digest` 是指定 private file 的 SHA-256。
完整消費 `iter_verified_records` 或呼叫 `verify_cutover_evidence` 才完成 records 驗證。
v1 diagnostics 只供 legacy inspection，不能供 v2 prepare/apply。

沒有 cleanup CLI。為每次工作建立單獨 private root 與 exact resource inventory；
只清理由該次工作產生、已核對 owner／路徑／用途的 artifacts，設定 cleanup deadline，
以獨立 filesystem/DB/Docker 查詢驗證不存在。不能遞迴清 workspace、共用 temp root、
backup repository、production volume 或 `projection_audit`；不可用 broad prune。
保留獨立 manifests、pins、receipts、失敗記錄至 operator 接受交接；逾時／刪除失败
列為 concern，不宣告 rehearsal 完整成功。DR runner 的 generated-resource cleanup
另有獨立 30-second deadline，見 [offsite DR](offsite-dr.md)。

## 1. Diagnose 與人工分類

以下 cutover commands 都從 `backend/` 執行。`DATABASE_URL` 只取 explicit process
environment；以核准的 secret channel 注入，不寫命令列、文件或 log。`$CUTOVER_DIR`
為 private absolute path；每個 output 是尚不存在的新路徑。

```bash
uv run python -m scripts.cutover_projection diagnose \
  --account-id "$ACCOUNT_ID" --environment "$CUTOVER_ENVIRONMENT" \
  --run-id "$RUN_ID" --image-digest "$IMAGE_DIGEST" \
  --projector-version execution-state-v1 --output "$CUTOVER_DIR/diagnostic"
```

Diagnose 在同一 REPEATABLE READ snapshot 取得 source table facts、event identity，
並 replay 至 temporary projections，保留 source 不變。人工逐筆查清 claim identity、
head/time、historical cycles、missing/unexpected symbols、checkpoint-source 差異。
允許分類為 `identity_representation`、`historical_state`、`time_sequence`、
`missing_symbol`、`checkpoint_source`；不能保留 `unexplained`，也不能用 blanket
classification 代替調查。

目前沒有 `classify` CLI。用既有 `EvidenceWriter.create(..., manifest_fields=...)`
寫 classification directory：kind=`projection-cutover-classification-v2`、format_version=2、
record_kind=`classification`、相同 run/scope/image/projector/stream、`diagnostic_digest`
及非空 `reviewer`。每個 record 保留 diagnostic 的 table/key_digest/column/
before_digest/after_digest，改 classification 並加非空 reason/evidence；次序一對一，
不得遗漏、重複或插入。呼叫 `finish()`，再以 `verify_cutover_evidence` 驗證兩個
directories 與全部 identity pins；只將 compact verified headers/counts 傳入 runtime。

已知 release gate：兩輪 completed historical CID 可以 genesis replay，但若 source
projection head 落後，apply 的 serialized append 可能先重播舊 cycle，觸發 claim
identity conflict。保留 halt；必須在 private rehearsal 證明該 exact source 可 apply。
不得自行 bump head、改歷史 event 或放寬 strict append 來繞過此 gate。

## 2. Prepare archive

```bash
uv run python -m scripts.cutover_projection prepare \
  --account-id "$ACCOUNT_ID" --environment "$CUTOVER_ENVIRONMENT" \
  --run-id "$RUN_ID" --image-digest "$IMAGE_DIGEST" \
  --projector-version execution-state-v1 \
  --managed-symbols fUST \
  --diagnostic "$CUTOVER_DIR/diagnostic" --diagnostic-digest "$DIAGNOSTIC_DIGEST" \
  --classification "$CUTOVER_DIR/classification" --classification-digest "$CLASSIFICATION_DIGEST" \
  --operations "$OPERATIONS_FILE" --operations-digest "$OPERATIONS_DIGEST" \
  --output "$CUTOVER_DIR/prepared"
```

**這個 CLI 會經 UUID vault/KEK 讀 credentials，並讀 Bitfinex offers/credits/wallets。**
無 venue 存取授權的 isolated rehearsal 不執行它；使用 pytest 的 synthetic snapshot
呼叫既有 `prepare_archive` API。`--dry-run` 只適用 prepare，rollback archive DB writes，
不代表不會讀 venue。Repeat prepare 另加 `--prepared-digest "$PREPARED_DIGEST"`，
仍使用同一 output、相同 artifacts，且 prepare snapshot 仍須新鮮。

Prepare 驗證 fresh complete coverage、runtime roles/quiescence、source stream/schema/
table digests；於 account lock 加 SHARE table locks 下只建立 immutable archive，
不 append event 或更換 active projection。小型 `projection-cutover-prepared-v1`
envelope 保留 codec/manifest 相容性，但其 diagnostic/classification pins 指向 v2。
檔案先寫、DB 後 commit；檔案存在不證明 commit 成功。可獨立核對：

```bash
uv run python -m scripts.cutover_projection verify-archive \
  --account-id "$ACCOUNT_ID" --environment "$CUTOVER_ENVIRONMENT" \
  --run-id "$RUN_ID" --image-digest "$IMAGE_DIGEST" \
  --projector-version execution-state-v1 \
  --output "$CUTOVER_DIR/prepared" --prepared-digest "$PREPARED_DIGEST"
```

注意這個 command 用 `--output` 指向既有 prepared file；它只驗 DB archive，
不產生 measured isolated-restore receipt。

## 3. Independently verify restored archive

先於 quiescent source 選 fresh backup/PITR target，建立獨立 schema-v2 RestoreBaseline，
包含所有 completed archive 的 `run_id/manifest_digest/prepared_path/prepared_digest`
references 與 `verifier_image_digest`，保留原始 prepared bytes；不能從待驗 restore
倒算 expected baseline。實作與格式見 [DR archive cutover extension](offsite-dr.md#projection-cutover-archive-verification)。

下列為已核准 operator 的真實 DR command，從 repo root 執行；它會使用 R2，
不是本地 pytest 命令：

```bash
deploy/vm/pgbackrest/restore-drill.sh \
  --account-id "$ACCOUNT_ID" --environment "$CUTOVER_ENVIRONMENT" \
  --projector-version execution-state-v1 --backup-label "$BACKUP_LABEL" \
  --baseline "$ARCHIVE_BASELINE_FILE" --archive-only --target-run-id "$RUN_ID"
```

若選 PITR，加 `--target-time "$TARGET_TIME"` 並與 baseline 完全一致；null target
必須維持所有 DB writers 停止直到 recovery 完成。Archive-only 驗證 exact archive
inventory、原始 prefix 與 target 的完整 event/schema identity，**不要求舊 active
projection parity**。它產生 v2 `kind=archive_restore` receipt，不能替代 full DR。

在已恢復的 isolated DB 上，底層 SELECT-only verifier 的實際命令為：

```bash
uv run python -m scripts.verify_projection_archive \
  --account-id "$ACCOUNT_ID" --environment "$CUTOVER_ENVIRONMENT" \
  --input "$ARCHIVE_INPUT_FILE" --input-digest "$ARCHIVE_INPUT_DIGEST" --archive-only
```

此處 DATABASE_URL 必須是 restored target。Input 由既有 `archive_evidence.transport`
產生：archive-only envelope schema_version=2，帶 target_run_id 與原始 prepared bytes
的 base64/SHA-256。保留與 DR receipt 相同的 transport digest，不能手編 measured receipt。

## 4. Fresh snapshot、atomic apply 與 repeat

完成 restore 後重新取得 explicit snapshot，並以 `--managed-symbols` 宣告本輪 scope。
本輪 recovery 使用 `--managed-symbols fUST`，因此 fUST 的 offers/credits/wallet 必須
完整覆蓋；fUSD 保持 dark，不可把缺少 fUSD snapshot 當成零。任何 scope 外 active
exposure 或非零 position 都要停止；若只剩所有 exposure/ledger buckets 與 `n_credits`
皆為零的 fUSD legacy scaffold，atomic rebuild 可移除該 inert row。缺 symbol/page/status/rate、
非 finite 數字、未知 exposure 或 uncertainty 也都要停止。Freshness 從 query start 起算，上限 300 秒，包含鎖等待與
transaction 執行時間。不能以舊 capture 更新 timestamp 偽造新鮮度。

目前沒有 snapshot CLI。核准的 collector 使用 `collect_snapshot`，以
`encode_row(serialize_event(snapshot))` 保存 private file 與 SHA-256；isolated pytest
只使用明確標示的 synthetic snapshot。Apply 本身不載 KEK、不做 HTTP、不 resume。

```bash
uv run python -m scripts.cutover_projection apply \
  --account-id "$ACCOUNT_ID" --environment "$CUTOVER_ENVIRONMENT" \
  --run-id "$RUN_ID" --image-digest "$IMAGE_DIGEST" \
  --projector-version execution-state-v1 \
  --managed-symbols fUST \
  --diagnostic "$CUTOVER_DIR/diagnostic" --diagnostic-digest "$DIAGNOSTIC_DIGEST" \
  --classification "$CUTOVER_DIR/classification" --classification-digest "$CLASSIFICATION_DIGEST" \
  --prepared "$CUTOVER_DIR/prepared" --prepared-digest "$PREPARED_DIGEST" \
  --receipt "$ARCHIVE_RECEIPT_FILE" --receipt-digest "$ARCHIVE_RECEIPT_DIGEST" \
  --archive-input "$ARCHIVE_INPUT_FILE" --archive-input-digest "$ARCHIVE_INPUT_DIGEST" \
  --snapshot "$SNAPSHOT_FILE" --snapshot-digest "$SNAPSHOT_DIGEST" \
  --operations "$OPERATIONS_FILE" --operations-digest "$OPERATIONS_DIGEST" \
  --output "$CUTOVER_DIR/applied"
```

CLI 在建 engine 前完成 bounded external file/evidence 驗證：prepared/archive-input/
snapshot 各 1 MiB，receipt/operations 各 64 KiB；不在 account lock 內重讀大目錄。
Runtime 要求 caller-owned READ COMMITTED transaction，先取 account advisory lock，
再核對 receipt，並以 SHARE ROW EXCLUSIVE table locks fence direct SQL writers。
CLI 設 lock_timeout=2s、statement_timeout=30s；這不是整段 transaction 的 global deadline。
在同一 transaction append 一個 fresh snapshot、由 genesis rebuild、驗 prefix/new head、
actual DB parity、archive preservation、quiescence/freshness，最後寫 applied archive 與 receipt。

Repeat 使用**完全相同**的 run/snapshot/artifacts/operations。已 committed request
驗 current stream、active/archive hashes、halt 與 quiescence 後回原 receipt，即使原
snapshot 已過期也不追加第二個 event；不允許換 snapshot 當成同一 request。

## 5. Rollback 與新 baseline

Commit 前的 exception 或 caller rollback 撤銷 event/projections/applied archive/receipt
的所有本次 DB row writes；已 committed 的 prepare archive 保留。PostgreSQL sequence
可能留下 gap，不能以連號推定成功或失敗。

Commit 後 output 寫入失敗／ack 遺失無法 rollback DB。保持 halt，先重送相同 pinned
request 驗證 DB receipt；若 state drift 則停止並 forward repair。任何 venue write
之後，禁止自動還原 old DB；遵循 [venue-write rollback](rollback-after-venue-write.md)，
fresh full-account reconcile 後由 operator 核准處理。

Apply 成功後從 quiescent **source** 獨立重新捕捉 event count/head/hash、migration heads
與 replay evidence，配對新的 backup/PITR target；不得直接把 apply receipt 當新 baseline。
將 receipt 的 `applied_manifest` 以相同 prepared envelope keys/codec 封装成獨立 private
archive expectation（保留其餘 binding fields），與所有舊 prepared references 一併 pin；
它是 DR archive transport，不是另一個可 apply 的 prepared request。

完整 restore command 不帶 archive-only/target-run-id：

```bash
deploy/vm/pgbackrest/restore-drill.sh \
  --account-id "$ACCOUNT_ID" --environment "$CUTOVER_ENVIRONMENT" \
  --projector-version execution-state-v1 --backup-label "$NEW_BACKUP_LABEL" \
  --baseline "$NEW_BASELINE_FILE"
```

Full transport 使用 schema_version=1、無 target_run_id；baseline 仍為 schema_version=2。
完整驗證所有歷史與 applied archives、原始 event prefixes、同 target 新 baseline 與
empty-projector/active parity；prepared-only 舊 projection 差異不應偽裝成 full DR 成功。

Release package 最後仍須列明未完成 gates：runtime UUID/KEK/owner 與 legacy-secret removal、
真實 auth denial、no uncertainty、fresh exposure、RPO/RTO、bounded canary 與兩次 fresh
reconciles。測試通過不代表其中任一 gate 已被執行或核准。
