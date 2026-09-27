# Projection archive：驗證與 atomic apply 契約

保留已完成 cutover 的 evidence 格式、archive restore 與原子性契約，供現行 DR 驗證
和歷史資料查核。日常部署見 [deploy](deploy.md)，停機與恢復見 [operations](operations.md)。

**`scripts/cutover_projection.py` 是已使用過的一次性工具，不能直接當成現行修復流程重跑。**
它的 `collect_snapshot` 仍只讀 offers／credits／wallets，未納入 funding loans；
operations probe 也沿用舊 compose topology。再次 prepare/apply 前，必須先補齊 loans
及逐列 wire 比對、更新並驗證現行部署的 writer inventory，再以 exact source 的
isolated rehearsal 證明完整性。本頁保留其契約，不提供舊 release 的執行清單。

## Scope、角色與停寫前提

Evidence 必須綁定 exact account UUID、environment、DB target、image/projector、
migration heads、backup/PITR target 與 event count/head/hash。Synthetic pytest 與舊
capture 不能冒充 fresh production snapshot；isolated rehearsal 不得有 venue writes。

- 實際 bot、webapi、frontend、weekly-report roles 必須逐一證明 archive
  INSERT/UPDATE/DELETE/TRUNCATE、column grants、ownership、schema CREATE 均不可達。
  包含 inherited／SET ROLE 路徑、PUBLIC 與 default grants；superuser、CREATEROLE、
  CREATEDB、REPLICATION、BYPASSRLS 都是 blocker。用無權限的 synthetic role 測試
  通過，不能證明真實 runtime role 已符合要求。
- Persistent halt 不等於 quiescence。必須停止所有 app、migration、報告及其他 DB writers，
  同時阻止 timer、restart policy、transient unit 或外部管理程序重啟它們；確認包含 idle
  sessions 的 DB clients。pgBackRest backup/status 維持運作以保留 RPO。
- 現行 app 是 `bfx-app`、資料服務是 `bfx`，而舊 `operations` codec/probe 預期
  `project=bfx` 與 autoheal 等歷史服務。不得靠偽造 inventory 讓舊 probe 通過；更新時需
  納入現行 `bfx-deploy.timer/service`、報告與所有 recreate sources。Immutable image、
  local daemon identity、實際 runtime roles 與 quiescence 的綁定仍須保留。

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

以下 Python commands 都從 `backend/` 執行。`DATABASE_URL` 只取 explicit process
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

已知 apply 邊界：兩輪 completed historical CID 可以 genesis replay，但若 source
projection head 落後，apply 的 serialized append 可能先重播舊 cycle，觸發 claim
identity conflict。保留 halt；必須在 private rehearsal 證明該 exact source 可 apply。
不得自行 bump head、改歷史 event 或放寬 strict append 來繞過此 gate。

## 2. Prepare 與既有 archive 驗證

**`prepare` CLI 會經 UUID vault/KEK 讀 credentials，並讀 Bitfinex offers/credits/wallets。**
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
已完成的 cutover 使用 `--managed-symbols fUST`，不代表未來操作可沿用同一 scope。
Managed symbols 的 exposure 與 wallet 必須完整覆蓋，缺少 symbol snapshot 不可當成零。
任何 scope 外 active
exposure 或非零 position 都要停止；若只剩所有 exposure/ledger buckets 與 `n_credits`
皆為零的 fUSD legacy scaffold，atomic rebuild 可移除該 inert row。缺 symbol/page/status/rate、
非 finite 數字、未知 exposure 或 uncertainty 也都要停止。Freshness 從 query start 起算，上限 300 秒，包含鎖等待與
transaction 執行時間。不能以舊 capture 更新 timestamp 偽造新鮮度。

目前沒有 snapshot CLI。核准的 collector 使用 `collect_snapshot`，以
`encode_row(serialize_event(snapshot))` 保存 private file 與 SHA-256；isolated pytest
只使用明確標示的 synthetic snapshot。Apply 本身不載 KEK、不做 HTTP、不 resume。

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

Archive 驗證或 apply 成功都不會恢復交易；後續 reconciliation、包絡與 operator resume
依 [operations runbook](operations.md) 執行。
