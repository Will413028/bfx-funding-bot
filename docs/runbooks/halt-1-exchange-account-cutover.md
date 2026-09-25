# Halt 1：ExchangeAccount identity cutover

> **歷史紀錄。** 這次切換已完成。文中的 canary gate、Halt 2、手動 image／migration 步驟屬於已刪除的
> 舊 release 流程；現行部署見 [deploy runbook](deploy.md)，停機與恢復見
> [operations runbook](operations.md)。第 6b 節的 webapi 手動 GRANT 已由 migration
> `c74d45a54e46` 取代（該 SQL 仍保留，供測試比對）。

這是 pre-launch 的 planned halt runbook。目的：把舊字串 realm 一次切換成
immutable `ExchangeAccount.id` UUID，並讓 money tables、credential vault、config
draft、API 與 daemon 使用同一個 account identity。本文件只定義人工 gate；agent
不會替 operator 連 production DB、解密 secret 或執行 migration。

## 不可妥協的完成條件

- 所有 legacy realm 已逐一列舉，並由兩人 review manifest；manifest 不含任何
  credential、JWT、password 或 plaintext secret。
- daemon 已停止寫入，persistent `trading_halt` 已確認有效。
- encrypted offsite backup 已完成，並在隔離資料庫 restore 成功；restore 後的
  row count、event head/hash 與 venue snapshot 都已保存 evidence。
- `cutover_identity.py --dry-run`、`--apply`、`--verify` 全部成功，且沒有
  `unmapped_rows`、duplicate active credential、invalid active credential、UUID
  collision 或 NULL UUID；verify 必須使用同一份 immutable evidence 並完成
  Bitfinex permission check。
- `9b2c3d4e5f6a` contract migration 成功；其 preflight 會拒絕任何 NULL UUID、
  orphan FK、未映射 realm，或 `users`/`executions`/`billing_records` 非零資料。
- replay、membership/auth boundary、fresh full-account venue reconcile 通過，
  才能開一個 account 的 canary gate。

任何 gate 失敗：維持 halt、保存輸出、停止流程。不要手動刪 row、跳過 preflight、
或以 `alembic downgrade` 當 rollback。

## 固定執行順序

### 1. 列舉 realm 與建立 evidence

在 stack host 建立僅 release operator 可讀的目錄。從 repo root 執行 read-only
查詢，逐一列出 `event_log` 等 money tables 的 distinct `account_id`、每個
environment 的 row count，以及 Better Auth user/account membership 對照。將結果
與此 repo 的 redacted manifest 比對，填入實際受控檔案（不要修改 example 檔）：

```bash
cd ~/bfx
mkdir -p release-evidence
chmod 700 release-evidence
cp deploy/vm/identity-realm-map.example.json release-evidence/identity-realm-map.json
chmod 600 release-evidence/identity-realm-map.json

docker exec --user postgres bfx-postgres psql -U bfx -d bfx -v ON_ERROR_STOP=1 -Atc \
  "SELECT 'event_log', account_id, deployment_environment, count(*)
     FROM event_log GROUP BY account_id, deployment_environment
     ORDER BY account_id, deployment_environment" \
  > "release-evidence/legacy-realm-counts-$(date -u +%Y%m%dT%H%M%SZ).txt"
```

### 2. Freeze writes，先做 backup + isolated restore

宣布 maintenance window，停止 deployment/reconcile submit path；保留 liveness 與
read-only admin probe。依 VM backup policy 產生 encrypted offsite backup，並把它
restore 到不連 Bitfinex 的隔離 Postgres。此處只允許 **legacy-schema-compatible**
backup/restore verification：對同一 frozen backup/PITR target 比對既有 schema、
legacy realm／environment counts、event head 與完整 event row integrity。
使用經 operator review 的 legacy restore 流程，只能還原至全新 generated volume，
保留 R2 recovery egress 直到 SQL role `bfx` 查詢 `pg_is_in_recovery()=false`，
然後斷開 egress；不能掛 production volume、application secret 或啟動 worker。
備份與 secret 安全規則見 [Offsite DR operator runbook](offsite-dr.md)；
該文件 UUID capture/replay 程序必須延後至
[post-identity DR gate](#post-identity-dr-gate)。本步的 pre-identity backup gate
仍須先通過，unit/timer 存在或 offline tests green 不能取代隔離還原證據：

```bash
git rev-parse HEAD > release-evidence/release-sha.txt
cd backend
uv run alembic current
```

此時 database 尚未套用 identity schema；不要在 additive revision 前把
`alembic check` 當成通過條件，否則它會正確地報出待套用的 identity operations。
`alembic check` 固定放在 contract migration 完成後的 Step 6。

對 frozen source 與隔離 restored copy 各執行下列 read-only SQL，完整比對輸出。
它僅依賴 legacy columns；`legacy_row_hash` 是同 schema/server 下的 row integrity
digest，不是後續 UUID replay 的 canonical event hash，不能供 Halt 2 使用。
SQL 以 OS user `postgres`、SQL role `bfx` 執行；每個 money table 的 realm/count
也必須與 Step 1 manifest 一致。任何缺列、額外 realm 或 digest mismatch 都停止：

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT account_id, deployment_environment, count(*) AS event_count,
       max(event_seq) AS event_head,
       encode(sha256(convert_to(
           string_agg(to_jsonb(e)::text, E'\n' ORDER BY event_seq), 'UTF8'
       )), 'hex') AS legacy_row_hash
FROM public.event_log AS e
GROUP BY account_id, deployment_environment
ORDER BY account_id, deployment_environment;
ROLLBACK;
```

同時以 Bitfinex REST 做一次 fresh **full-account** reconcile（offers、credits、
wallet availability，不只 strategy symbols），把 normalized snapshot 與 query
時間保存到 evidence；snapshot 不含 API headers 或 secret。

### 3. Persist halt，停止 worker

先透過現有 operator admin flow 寫入 account-scoped halt，確認
`GET /admin/trading-status` 的 `halt.halted=true` 且 `deploy-vm/halt-watch.sh`
回報 `halt CONFIRMED`。再停止唯一 daemon worker，確認沒有新的
`RESERVATION_INTENT`：

```bash
cd ~/bfx
docker compose --env-file .env.runtime -f docker-compose.bot.yml stop bot
docker exec --user postgres bfx-postgres psql -U bfx -d bfx -Atc \
  "SELECT count(*) FROM event_log
     WHERE event_type='RESERVATION_INTENT'
       AND occurred_at_ms >= (EXTRACT(EPOCH FROM now())*1000)::bigint - 20*60*1000"
```

### 4. 只套用 additive schema

此時只到 `8a1b2c3d4e5f`；不要先跑 `upgrade head`，因為 head 會進入 contract
gate，而 contract 必須等 application backfill verify 後才可執行：

```bash
cd ~/bfx/backend
uv run alembic upgrade 8a1b2c3d4e5f
uv run alembic current
```

### 5. Dry-run → apply → verify identity data cutover

使用已 review 的實際 manifest。KEK 從受控 secret store 注入
`BFX_VAULT_KEK`；stdout、JSON report 與 shell history 都不得出現 secret。先
dry-run，review aggregate counts/hash 後才 apply；apply 可安全重跑，但每次都要
保留 report：

```bash
cd ~/bfx/backend
MANIFEST=../release-evidence/identity-realm-map.json

uv run python scripts/cutover_identity.py --manifest "$MANIFEST"
uv run python scripts/cutover_identity.py --manifest "$MANIFEST" --apply --dry-run \
  | tee ../release-evidence/identity-dry-run.json

# 只有 dry-run report 經 review 後：
uv run python scripts/cutover_identity.py --manifest "$MANIFEST" --apply \
  | tee ../release-evidence/identity-apply.json
uv run python scripts/cutover_identity.py --manifest "$MANIFEST" --verify \
  --evidence ../release-evidence/identity-dry-run.json --verify-permissions \
  | tee ../release-evidence/identity-verify.json
```

`verify` 必須確認每個 money table、`api_keys`、`user_configs` 都是 UUID-ready，
credential 以 account UUID AAD 可解密，舊 user AAD 不能解密，target/source counts
一致。任何 partial failure 都是 transaction rollback；不要直接改 SQL 繞過。

### 6. 套用 contract migration

application verify 與 backup evidence 通過後才執行。migration 自己會再做一次
preflight，因此在 DDL 前仍可安全拒絕：

```bash
cd ~/bfx/backend
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

成功結果包括 UUID `NOT NULL`、`exchange_accounts` 的 `ON DELETE RESTRICT` FK、
以 UUID 重建的 primary/unique indexes，以及只在 zero-row gate 後刪除三個 legacy
scaffold tables。contract revision 是 forward-only。

### 6a. Bootstrap legacy environment credential（僅 vault 原本為空時）

若舊 daemon 使用環境變數中的 key/secret，而 `api_keys` 沒有資料，identity
backfill 不會憑空產生 credential。Step 5 verify 也不能取代此處的實際 key
permission verification。完成 contract migration 後、post-identity backup 前，
保持所有 writer 停止，由 operator 將原有秘密注入一次性程序的環境：
`BFX_BOOTSTRAP_API_KEY`、`BFX_BOOTSTRAP_API_SECRET`、既有 `BFX_VAULT_KEK`、
owner DB role 的 `DATABASE_URL`。不可把秘密放入 CLI arguments、shell history、
tracked files、Docker image 或 log；不要使用 `set -x` 或輸出 container inspect。

工具不讀 dotenv，不建立 account/membership，不輪替既有 credential。目標 UUID
及 owner 必須先對照已 review 的 identity manifest；Bitfinex permission check
只證明 key 的權限，不能證明 key 屬於正確的實際帳號，仍須人工核對來源及
fresh full-account venue snapshot。預設 dry-run 會實際查詢 Bitfinex permissions，
在 DB transaction 內建立／驗證後 rollback，並非完全沒有外部請求。

```bash
cd backend
# UUID 與 OWNER_USER_ID 是非秘密的已核對 manifest 值；秘密由 operator 安全注入。
uv run python scripts/bootstrap_account_credential.py \
  --exchange-account-id "$UUID" --owner-user-id "$OWNER_USER_ID"
# dry-run 回報 verified 且 applied=false，operator review 後才可執行：
uv run python scripts/bootstrap_account_credential.py \
  --exchange-account-id "$UUID" --owner-user-id "$OWNER_USER_ID" --apply
```

apply 會重新驗證 funding-write、拒絕其他 write scopes，再以 UUID AAD 加密的
credential 提交；錯誤只回傳固定失敗訊息，不輸出 upstream error 或 traceback。
已有任何 credential（含 revoked/retired）會拒絕執行，不會覆寫或復活舊 key。
若 apply 連線中断而結果不明，先唯讀確認 vault 狀態，不可手動刪除再重跑。
完成此步不代表可 restart worker；仍須 grants、post-identity DR/replay、runtime
KEK/UUID 設定、config 與 venue reconciliation 等後續 gate 全部通過。

### 6b. 套用 webapi least-privilege grants

migration `c74d45a54e46` 起，下列 baseline 由 migration 授予（`test_the_web_api_baseline_is_granted_by_migration_not_by_hand` 驗證），不再需要手動執行；以下保留作舊環境對照。contract migration 後，以 database owner/superuser 執行一次性 grant。webapi 只
能讀 projection、account/membership，並管理自己的 credential/config draft；不得
寫入 event store、position、offer 或其他 execution tables。不要把這些 grant 放進
Alembic，避免 role 不存在時讓 migration 失敗：

```sql
GRANT USAGE ON SCHEMA public TO bfx_webapi;
-- /ready compares the database revision with the image's Alembic graph.
-- Read the version column only; no migration/write authority is granted.
GRANT SELECT (version_num) ON TABLE public.alembic_version TO bfx_webapi;
GRANT SELECT ON TABLE
  public.user_profiles,
  public.exchange_accounts,
  public.exchange_account_memberships,
  public.position_state,
  public.offer_claims,
  public.event_log,
  public.execution_uncertainties,
  public.submission_attempts,
  public.attribution_weekly,
  public.funding_candles
TO bfx_webapi;
GRANT INSERT ON TABLE public.user_profiles TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE ON TABLE public.exchange_account_credentials TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.account_config_drafts TO bfx_webapi;
-- ADR D4' operator adjudication outbox (migration 1c435a35dcb4 grants the same):
-- read the queue, insert request columns only; the daemon applies them.
GRANT SELECT ON TABLE public.uncertainty_resolution_requests TO bfx_webapi;
GRANT INSERT (request_id, exchange_account_id, deployment_environment, uncertainty_id,
  action, reconcile_event_seq, venue_offer_id, decision, reason, requested_by,
  created_at_ms) ON TABLE public.uncertainty_resolution_requests TO bfx_webapi;
-- Release-flow approve/resume outbox, same contract (migration 1c435a35dcb4 grants the same).
GRANT SELECT ON TABLE public.trading_state, public.deployment_approvals,
  public.trading_control_requests TO bfx_webapi;
GRANT INSERT (request_id, exchange_account_id, deployment_environment, action, backend_digest,
  reason, requested_by, created_at_ms) ON TABLE public.trading_control_requests TO bfx_webapi;

-- Run as the role owner and retain the output as release evidence.
SET ROLE bfx_webapi;
SELECT has_table_privilege(current_user, 'public.position_state', 'SELECT') AS can_read_position,
       has_table_privilege(current_user, 'public.execution_uncertainties', 'SELECT') AS can_read_uncertainties,
       has_table_privilege(current_user, 'public.submission_attempts', 'SELECT') AS can_read_attempts,
       has_table_privilege(current_user, 'public.event_log', 'INSERT') AS can_write_event,
       has_table_privilege(current_user, 'public.exchange_account_credentials', 'UPDATE') AS can_update_credentials;
RESET ROLE;
```

預期結果為 `can_read_position=t`、`can_read_uncertainties=t`、`can_read_attempts=t`、`can_write_event=f`、
`can_update_credentials=t`；任何其他 privilege 都停止 release，先修正 role。

`uncertainties` list/detail 需要讀取 uncertainty projection 與 submission attempt
才能建立 server-derived resolution context；空表也需要 SELECT 權限。這兩張表
只授予 SELECT，不授予 INSERT/UPDATE/DELETE/TRUNCATE。既有環境若缺少這兩項
讀取權限，可由 role owner 補授相同的 SELECT，無須重啟服務或更動 schema head。
以受限角色驗證 API 的空集合與含 UNKNOWN 情境，不能只用 database owner 測試。

啟動後另驗 webapi `/ready` 回 200。只驗 `SELECT 1` 不足以證明 schema
readiness：以 `bfx_webapi` 實際讀取 `alembic_version.version_num`，並確認仍無
`alembic_version` UPDATE、`event_log` INSERT 與 `capital_policy_revisions` INSERT
權限（後者於 capital migration 後檢查）。不要用 database-owner URL 代替受限
角色，也不要因 readiness 失敗而授予全部表讀寫權限。

<a id="post-identity-dr-gate"></a>

### 7. Post-identity DR、Replay、auth boundary 與一帳號 canary gate

Step 4 additive identity migration、Step 5 cutover verify、Step 6 contract
migration、必要的 Step 6a bootstrap 與 Step 6b grants 全部通過後，維持 all-writer quiescence，重新建立
post-identity backup。此時才可依 [canonical UUID baseline capture](offsite-dr.md#6-capture-the-same-target-bounded-baselinejson)
及 [staged UUID restore/replay](offsite-dr.md#7-run-the-staged-isolated-restore-with---baseline)
為 same backup/PITR target 取得 fresh measured evidence。逐欄核對 UUID、
environment、migration heads、event count/head/hash 及 projections，確認 recovery
完成後才 disconnect egress，且 generated verifier/container/volume/networks
cleanup 成功。不得重用 Step 2 legacy backup 或將其 integrity digest 當作
canonical UUID baseline；本 gate 通過前不得 restart worker 或進 Halt 2。

在 worker restart 前做 read-only replay/rebuild，確認 event head/hash、projection
counts、membership 404/403 行為與 explicit URL contract。確認 daemon env 只有
`BFX_EXCHANGE_ACCOUNT_ID=<canonical UUID>`；不存在 `BFX_ACCOUNT_ID`、
`BFX_API_KEY` 或 `BFX_API_SECRET` fallback。再以同一 account 做 fresh full-account
venue reconcile，確認 DB exposure 與 venue authority 一致。

最後只開一個 account 的 canary，保留 halt-watch 與 deployment logs；若任何
credential、projection、venue reconcile、auth 或 readiness probe 異常，立即再次
persist halt。canary gate 未通過前不得擴大 cap 或開第二個 account。

## Rollback / incident posture

這次 contract migration 不提供 downgrade：它可能已移除 zero-row legacy tables，
而且 DB rollback 不能撤銷已發生的 venue mutation。若需要 rollback：保持 halt，
保留目前 DB/venue evidence，從 verified backup/PITR restore 到隔離環境，先確認
restore 時間點之後沒有 venue writes；若有，先以 venue reconcile 建立 forward-fix
計畫，再恢復 worker。所有決定與 report 寫入 incident evidence。
