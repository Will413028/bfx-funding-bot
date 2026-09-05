# Offsite DR Foundation Design

**Status:** Approved — foundation implemented; hardening implementation in progress
**Date:** 2026-09-04
**Design reference:** Repository-local offsite DR implementation plan

## Goal

為目前 OCI VM 上的 PostgreSQL 18 建立可重現、secret-safe 的異地備份基礎：
以 pgBackRest 將 continuous WAL 與週期性 base backup 存到 Cloudflare R2，
再以不連 Bitfinex 的 isolated restore drill 驗證 event chain 與
event-only projection replay。這一階段不執行 production rollout，也不宣稱
RPO/RTO 已達標，直到實測 evidence 產生。

## Context and constraints

- production DB 是 `docker-compose.bot.yml` 的 `postgres:18-alpine`，資料持久化在
  `bfx_pgdata`，目前沒有 `archive_mode`、WAL offsite archive 或 restore automation。
- Halt 2 的 preflight 已要求 backup evidence、isolated-restore evidence、event
  head/hash 與 projection replay；目前 `_read_dr_measurement()` 已固定接受
  `{"measured": true, "rpo_seconds": N}` 與
  `{"measured": true, "rto_seconds": N}`。
- operational target 是 RPO ≤300s、restore drill RTO ≤3600s；Halt 2 canary
  gate 的更嚴格 restore RTO ≤60s 維持不變，不能用一般 DR target 取代。
- R2 S3 API 的 endpoint 是 `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`、region
  是 `auto`；pgBackRest 2.59.1 支援 PostgreSQL 18、S3 repository、async archive
  與 client-side repository encryption。
- R2 目前不支援 S3 Object Lock request headers。R2 Bucket Lock 是另一套 bucket
  policy，且可能與 pgBackRest 的 object expiration delete 互相衝突；因此不能把
  WORM 宣稱當作本設計的前提。

## Options considered

### A. Custom PostgreSQL image with pgBackRest (recommended)

以目前 PostgreSQL image 為 base，在同一個 DB container 安裝 pinned pgBackRest。
PostgreSQL 的 `archive_command` 與 online backup 都在能直接存取 `PGDATA` 的環境
執行；R2 credentials 以 container 外部掛載的 `conf.d` secret file 注入。

優點是 archive path、backup path、restore path 只有一套 binary/config，且不需
把 Docker socket 給 backup sidecar。代價是 image build 與 PostgreSQL container
recreate 要在 maintenance window 驗證。

### B. Host-installed pgBackRest with a remote PostgreSQL protocol

在 OCI host 安裝 pgBackRest，透過 SSH/TLS protocol 操作 container 內的 PostgreSQL。
這可避免改 PostgreSQL image，但增加 host/container protocol、權限、certificate
與 restore path 的維運面，對單一 VM 沒有足夠收益。

### C. Application-level `pg_dump` uploader

保留現有 image，用 shell/Python 定期 `pg_dump` 後上傳 R2。實作較快，但沒有
continuous WAL/PITR，難以滿足 RPO ≤5m，也容易把 event-sourced DB 的 restore
驗證誤縮成 dump 成功。

**Decision:** 採 A。以 pgBackRest 作為 backup data-plane；只寫小型 shell wrapper
負責 preflight、schedule、redacted evidence 與 isolated drill，不自行重寫 backup
格式或 retry/retention 語義。

## Architecture

```text
PostgreSQL 18 container
  ├─ archive_mode=on
  ├─ archive_command → pgbackrest archive-push
  ├─ pgBackRest config (tracked, no secrets)
  └─ mounted secret conf.d (R2 key + repository cipher pass)
             │
             ▼
      R2 private bucket /pgbackrest/
      WAL + weekly full + daily differential
             │
             ▼
  isolated restore volume
    ├─ temporary R2 egress network (restore-db only, restore phase)
    └─ DR internal network (restore-db + verifier; egress disconnected before verifier)
             │
             ├─ schema / row-count / pgBackRest verification
             └─ verify_projection_replay.py → event/projection hashes
```

## Approved hardening revision (2026-09-05)

The first implementation exposed load-bearing gaps between the RPO claim, the
R2 egress boundary, and the behavior of a restored non-empty PostgreSQL data
directory. The following revision is part of this design and is required before
the foundation can be considered review-ready.

1. **RPO boundary:** production PostgreSQL sets `archive_timeout=60s` alongside
   `archive_mode=on` and `archive_command`. A low-write workload must not wait
   indefinitely for a full WAL segment before archiving.
2. **Runtime identity and logging:** every pgBackRest/psql operation executed
   through `docker exec` uses `--user postgres`. The tracked pgBackRest config
   disables persistent file, console, and stderr logging; evidence and bounded
   exit codes are the diagnostic boundary, so R2 endpoint/bucket values cannot
   enter host logs.
3. **Staged DR networking:** the generated DR internal network is created with
   `--internal` and is used by `restore-db` and `verifier`. A second generated
   egress network is created without `--internal` and is attached only to
   `restore-db` during pgBackRest restore/recovery. The runner must verify the
   two network properties, disconnect egress after the restore container is
   healthy and SQL role `bfx` confirms `pg_is_in_recovery()` is false within
   the same global deadline. Require membership to be exactly the generated
   internal network before bootstrap/verifier; then remove both generated networks.
   The verifier never receives R2 egress.
4. **Restored-cluster bootstrap:** `POSTGRES_USER`, `POSTGRES_PASSWORD`, and
   `POSTGRES_DB` are not treated as initialization controls for restored PGDATA.
   After recovery completion and egress disconnect, the runner connects through
   the local socket as OS user `postgres`, SQL admin role `bfx`, validates the
   operator-selected existing database, and
   creates a generated ephemeral verifier role with only `CONNECT`, `TEMPORARY`,
   schema `USAGE`, and the fixed event/projection/migration `SELECT` grants.
   The verifier receives only a temporary connection URL; production app
   secrets are never mounted into the DR Compose project.
5. **Expected-state baseline:** every measured drill requires a bounded,
   operator-supplied baseline artifact bound to the selected backup label and
   PITR target. It contains the restored database name, account/environment/
   projector identity, migration heads, event count, event head, and canonical
   event hash. The drill passes the expected hash to
   `verify_projection_replay.py` and compares every baseline field before
   writing measured success evidence. Missing or mismatched baseline is a
   failed drill.
6. **Evidence and lifecycle:** restore evidence includes `observed_at_ms` and
   the validated `target_time`, uses the immutable local container image ID plus
   pinned image labels, and is accepted only when fresh. All subprocesses share
   one monotonic 3600-second deadline; cleanup has its own short deadline and
   removes only generated resources. Any status/preflight failure atomically
   replaces prior evidence with `measured:false`; persistence failure must remove
   or truncate stale green evidence, including ENOSPC.

The Compose volume key is always the tracked logical key `restore-data`; its
external Docker volume name is supplied through `DR_VOLUME_NAME`. This keeps
the rendered Compose model valid while retaining generated-resource ownership
in the runner.

### PostgreSQL and image boundary

Final broad-review contracts: actual Compose subprocesses receive sanitized env
without ambient `DR_*`, `DATABASE_URL`, `POSTGRES_*`, `BFX_*` or `COMPOSE_*`;
generated interpolation/verifier credentials are authoritative while PATH and
Docker transport remain available. The verifier has a validated generated
container name and cleanup eligibility recorded before the run attempt. Cleanup
independently force-removes that container before removing networks, tolerating
normal auto-removal only after a successful exact-name absence check, within
the independent 30-second budget.

Pinned 2.59.1 info timestamps are strict integer epoch `timestamp.start/stop`;
bool/float/string/negative/reversed timestamps fail closed. Both stanza and
repository status require integer zero, stanza `bfx` and repository key 1 with
the approved cipher. Smoke captures all stdout/stderr privately with trap cleanup
and emits fixed stage/error markers only; no raw info JSON is printed.
Backup/status/preflight mark refresh incomplete before collecting; persistence
errors invalidate/truncate/remove prior green evidence, including ENOSPC.

Halt 1 retains a pre-identity legacy-schema-compatible realm/count/event-integrity
backup/restore gate. The canonical UUID baseline and staged UUID replay gate is
linked from its post-identity step, after additive migration and cutover
verification (and contract/grants); legacy evidence cannot satisfy Halt 2.

- 建立 `deploy/vm/postgres/Dockerfile`，base 為 PostgreSQL 18 Alpine 的 pinned
  digest；明確保留 `PGDATA=/var/lib/postgresql/18/docker` 與現有
  `/var/lib/postgresql` volume mount。
- 以 upstream pgBackRest 2.59.1 distribution tarball 建置 binary，並在 build
  時核對 SHA-256；不使用 floating third-party pgBackRest image。
- `postgres` service 改用這個 image，啟動參數加入
  `archive_mode=on`、`archive_timeout=60s` 與
  `archive_command='pgbackrest --stanza=bfx archive-push %p'`。
  `archive-async=y`、spool path 與 log path 都在不屬於 `PGDATA` 的 volume。
- 維持現有 PostgreSQL healthcheck，不把 R2 lag 直接接到 autoheal。R2 故障時
  應由 backup status/alert 顯示 fail-closed，而不是讓 autoheal 反覆重啟資料庫。
- `docker-compose.bot.yml` 的 `migrate`、bot、webapi 依賴關係不變；本 change
  不改 application event model 或 projection code。

### pgBackRest configuration

追蹤的 `deploy/vm/pgbackrest/pgbackrest.conf` 只包含非 secret 設定，並由預設的
`/etc/pgbackrest/conf.d/*.conf` 載入受控 secret fragment。secret fragment 不在
repo，VM 上由 operator 建立並以最小權限掛載。

固定設定：

- stanza `bfx`，`pg1-path=/var/lib/postgresql/18/docker`、`pg1-user=bfx`；
  SQL admin role 固定為 deployed cluster 既有的 `bfx`，與 OS user `postgres` 分開。
- `repo1-type=s3`、`repo1-s3-region=auto`、`repo1-s3-uri-style=path`、R2
  account endpoint，以及 bucket prefix `/pgbackrest`。
- `repo1-block=y`、`repo1-bundle=y`、`archive-async=y`、`start-fast=y`；object
  store compatibility smoke test 不通過時不得 deploy。
- `repo1-cipher-type=aes-256-cbc`；cipher passphrase 與 R2 access key/secret
  分開保存。pgBackRest 的 repository encryption 是 client-side，不依賴 R2
  server-side encryption。
- 初始 retention：每週日 03:17 UTC full、其餘日 diff；保留 4 組 full 與每組
  6 組 differential。archive retention 不另行壓縮，先跟隨尚未過期 backup；
  實測容量與 restore 時間後再調整。
- `log-level-file=off`、`log-level-console=off`、`log-level-stderr=off`；
  不把 pgBackRest raw diagnostics 寫入 persistent log，wrapper 只保留
  allowlisted error code 與 exit status。

R2 token 只使用 bucket-scoped Object Read & Write，因 pgBackRest 需要 list/read/
write/delete/multipart object operations；不使用 account-admin token。bucket
維持 private Standard。pgBackRest prefix 不啟用未經 smoke test 的 S3 Object Lock；
若日後使用 R2 Bucket Lock，必須先證明不會令 `expire` 永久失敗，並把它視為額外
retention control 而非已驗證 WORM。

### Secret and evidence boundary

- R2 endpoint/bucket、R2 Access Key ID、Secret Access Key、repository cipher pass
  全在 VM secret boundary；不進 git、CI variables、shell history、Docker image
  layer 或 evidence JSON。
- `conf.d` secret directory/file 必須是非 symlink；direct regular files 只能用
  `.conf` suffix，每檔只有單一 `[global]` section，拒絕 section 外 assignment、
  wrong/extra/repeated sections 與 parser ambiguity。只含且各含一次
  `repo1-s3-endpoint`、`repo1-s3-bucket`、`repo1-s3-key`、
  `repo1-s3-key-secret`、`repo1-cipher-pass` 五個 options；container postgres
  UID/GID 可讀取，其他使用者不可讀寫，並以 read-only bind mount 注入。
  deploy 與 restore preflight 共用同一個 validator，拒絕空值、示意值、重複
  options 與未知 secret options。
- cipher passphrase 必須另行 offline escrow；遺失它等同遺失 encrypted repository
  的可讀性。R2 token rotation 與 cipher rotation 是不同 runbook，不在本 change
  自動處理。
- 所有 evidence 只寫固定欄位、counts、timestamps、backup label/LSN、hash 與
  bounded error；禁止 raw event payload、DB password、API key、Authorization
  header 與 Bitfinex response。

### Status, preflight, and measurement

新增 `deploy/vm/pgbackrest/status.sh`（read-only）與 `preflight.sh`（會執行一次
明確的 pgBackRest archive check）。status 讀取：

- PostgreSQL `pg_stat_archiver` 的 last archived WAL/time、failed count/time。
- `pgbackrest info --output=json` 的最新 backup metadata。
- config/image/version identifiers 與固定 repository/stanza identity。

preflight 額外執行 `pgbackrest check`，利用 pgBackRest 的 archive check 產生可驗證
的新 WAL archive，然後計算 observed `rpo_seconds`。任何 credentials 缺失、R2
operation 失敗、archive lag >300s、無完整 backup set 或輸出無法解析，都回傳
nonzero；不可把 unavailable 當作 0，也不可只用 `pg_dump` 成功當作 RPO evidence。

backup evidence 最少為：

```json
{
  "schema_version": 1,
  "measured": true,
  "rpo_seconds": 42,
  "observed_at_ms": 0,
  "stanza": "bfx",
  "repository": "r2",
  "last_archived_wal": "<bounded-wal-name>",
  "latest_backup_label": "<pgbackrest-label>",
  "config_digest": "<sha256>"
}
```

### Isolated restore drill

新增 `docker-compose.dr.yml` 與 `deploy/vm/pgbackrest/restore-drill.sh`：

1. operator 指定一個已存在的 backup/PITR target；script 僅建立帶有隨機、明確
   prefix 的 temporary restore volume，不讀寫 `bfx_pgdata`。
2. restore container 只掛 pgBackRest config、R2 secret 與 temporary volume；不掛
   `bot.env`、`webapi.env`、`frontend.env`、`BFX_VAULT_KEK`，不加入 production
   Docker network，也不啟動 daemon、webapi 或 Bitfinex client。restore-db 暫時
   同時加入 generated R2 egress network 與 generated DR internal network；只要
   restore-db healthcheck 通過且 SQL role `bfx` 於既有 baseline database 查得
   `pg_is_in_recovery()=false` 才斷開 egress；true 必須在 global deadline 內輪詢，
   invalid output／timeout 均 fail closed。
3. verifier 只加入 generated DR internal network；它執行 schema/version、
   row-count、event head/hash，再以現有
   `verify_projection_replay.py replay` 從 empty temporary projection 重建並比較
   projection content hashes。還原後的既有 database/role 不由
   `POSTGRES_*` environment 初始化，改由 runner 建立 ephemeral least-privilege
   verifier role。
4. script 量測 restore start 到所有 verifier gate 成功的 elapsed seconds，寫出
   `{"measured": true, "rto_seconds": N, ...}` 的 redacted restore evidence。
5. 成功或失敗都只清理 script 建立且名稱經驗證的 DR resources；失敗保留 evidence
   與 bounded logs，絕不觸碰 production volume。任何 event/projection hash mismatch、
   schema mismatch、network isolation failure 或 verifier nonzero 都是 failed drill。

restore evidence 最少包含 `schema_version`、`measured`、`rto_seconds`、
`observed_at_ms`、validated target backup label/time、isolated network assertion、
event head/hash、projection hashes、row counts、verifier exit status、config digest
與 immutable image ID/labels。它可直接被目前 Halt 2 `_artifact_hashes()` 讀取；
`rto_seconds > 60` 仍會被 Halt 2 preflight 拒絕。沒有 expected-state baseline、
stale evidence、egress 尚未斷開或任何 cleanup failure 都不得產生 measured success。

### Scheduling and operator controls

- 新增 `bfx-pgbackrest-backup.service/.timer`，每天 03:17 UTC 呼叫 one-shot
  wrapper；wrapper 依 UTC weekday 選 full 或 diff，使用
  `docker exec --user postgres bfx-postgres`
  而非 `docker compose run`，避免繼承 app service 的 healthcheck/autoheal。
- 新增 `bfx-pgbackrest-status.service/.timer`，每 5 分鐘產生 status/evidence，
  但不自動 restore、resume、刪除 repository 或改 halt state。
- `scripts/deploy-vm.sh` 在 compose build/up 前檢查 pgBackRest config、secret
  directory、R2 bucket identity 與 pinned image metadata；初次 stanza-create、
  R2 token 建立、bucket policy 與實際 production rollout 仍由 operator 依 runbook
  執行。
- monthly restore drill 是人工確認的 maintenance task，不由 production timer
  自動對正式 DB 做 restore。

## Error handling and safety invariants

- 所有 wrapper 都使用 `set -euo pipefail`；缺檔、placeholder、R2/auth failure、
  nonzero pgBackRest、stale archive、無法建立 isolated network 或 verifier
  nonzero 都 fail closed。
- `status`/`preflight` 對 command output 先驗證格式，再產生 bounded JSON；任何
  capture failure 只輸出 `unavailable` 與 exit status，不輸出 secret。
- backup/restore evidence 的 file digest、`rpo_seconds`、`rto_seconds` 與目前
  Halt 2 artifact contract 必須一致；hash mismatch、measurement missing 或 stale
  report 不得被手動 JSON override。
- restore drill 與 production deploy 分離；DB restore 永遠不等於 venue rollback，
  仍遵守 `rollback-after-venue-write.md` 的 halt/reconcile/forward-fix 規則。

## Testing strategy

先以 TDD 寫 offline contract tests，再做 opt-in integration smoke：

- config contract test：確認 PGDATA、stanza、R2 endpoint/region、encryption、
  archive timeout/command、retention、log suppression 與 secret fragment boundary；tracked files 不得含
  credential literal 或可被直接載入的 secret config。
- compose/systemd contract test：確認 custom image、volume mount、archive command、
  timer schedule、one-shot 不帶 autoheal，及 production healthcheck 不會因 archive
  lag 觸發 restart。
- report contract test：用 unavailable、stale、failed archive、malformed JSON、
  `rpo_seconds > 300`、`rto_seconds > 60` fixture 驗證 fail-closed 與現有
  `halt2_cutover` parser 相容。
- restore command-builder test：確認 temporary volume/network 名稱只接受受控值、
  `restore-data` logical volume 與兩個 generated networks 的 staged lifecycle、
  不會引用 `bfx_pgdata` 或 production env files，並在任何步驟 failure 時保留
  evidence。
- restore baseline/auth test：確認 baseline 欄位與 selected target 完全一致、
  expected event hash 進入 verifier argv、ephemeral role bootstrap 只使用 local
  `--user postgres` socket path，且 secret 不進 command output/evidence。
- lifecycle evidence test：確認 local image ID/labels、`observed_at_ms`、target
  time、freshness、global command deadline、independent cleanup deadline，以及
  failure 時 measured:false 會取代舊的 measured:true report。
- opt-in operator smoke：用真實 bucket/token 執行 `stanza-create`、`check`、full
  backup、diff backup、`info`、`verify`；再執行 isolated restore。這些不進預設 CI，
  不把 credential 帶入測試，也不在 coding-agent session 自動對 production 執行。
- 完成時執行 repo 規定的 `cd backend_py && uv run pytest -m "not integration"`、
  `uv run mypy src/`、`uv run ruff check`，以及 Docker Compose/config 與 shell
  syntax checks；實際 RPO/RTO evidence 另由 operator drill 產生。

## Non-goals

- 不建立第二個 immutable provider、不做自動 failover/standby replication。
- 不把 Cloudflare dashboard、R2 token、cipher escrow 或 VM secret provisioning
  自動化成 repo code。
- 不修改 application event schema、projection、venue reconcile 或 Halt 2 state
  machine。
- 不在本 change 執行 production migration、restart、restore、resume、Bitfinex
  request 或 cap change。

## Acceptance criteria

1. Custom PostgreSQL image 可在 clean local build 中印出 pinned PostgreSQL/pgBackRest
   versions，且 compose config 保持 `/var/lib/postgresql` data volume。
2. R2 config、secret boundary、systemd schedules、status/preflight、restore drill
   與 report schema 都有 offline tests；default test 不需要 credentials。
3. Operator smoke 證明 R2 支援 pgBackRest 所需的 list/head/read/write/multipart/
   delete lifecycle，且 `stanza-create`、archive check、full/diff backup 與
   `info/verify` 成功。
4. Isolated restore drill 成功，event head/hash 與 empty-projector replay hash
   一致且符合 expected-state baseline；restore evidence 可被現有 Halt 2 artifact
   parser 接受，並證明 verifier 在 egress 斷開後仍只位於 internal network。
5. 連續觀測後才記錄實測 RPO/RTO；未達 target 時維持 halt、修正 backup/restore，
   不以 CI green、config presence 或 backup file existence 宣稱 DR ready。

## References

- [Cloudflare R2 S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
- [Cloudflare R2 authentication and bucket-scoped tokens](https://developers.cloudflare.com/r2/api/tokens/)
- [Cloudflare R2 Bucket Locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/)
- [pgBackRest 2.59.1 release notes](https://pgbackrest.org/release.html)
- [pgBackRest configuration reference](https://pgbackrest.org/configuration.html)
- [pgBackRest user guide](https://pgbackrest.org/user-guide.html)
- [PostgreSQL 18 `archive_mode`/`archive_command`](https://www.postgresql.org/docs/18/runtime-config-wal.html)
