# Halt 1：ExchangeAccount identity cutover

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

docker exec bfx-postgres psql -U bfx -d bfx -v ON_ERROR_STOP=1 -Atc \
  "SELECT 'event_log', account_id, deployment_environment, count(*)
     FROM event_log GROUP BY account_id, deployment_environment
     ORDER BY account_id, deployment_environment" \
  > "release-evidence/legacy-realm-counts-$(date -u +%Y%m%dT%H%M%SZ).txt"
```

### 2. Freeze writes，先做 backup + isolated restore

宣布 maintenance window，停止 deployment/reconcile submit path；保留 liveness 與
read-only admin probe。依 VM backup policy 產生 encrypted offsite backup，並把它
restore 到不連 Bitfinex 的隔離 Postgres。只在隔離 restore 上驗證 schema、row
counts、`event_log` head 與 application event hash；不要把 production secret 帶到
restore：

```bash
git rev-parse HEAD > release-evidence/release-sha.txt
cd backend_py
uv run alembic current
```

此時 database 尚未套用 identity schema；不要在 additive revision 前把
`alembic check` 當成通過條件，否則它會正確地報出待套用的 identity operations。
`alembic check` 固定放在 contract migration 完成後的 Step 6。

隔離資料庫上的 event evidence 至少包含：每個 account/environment 的 count、
最高 `event_seq`、`cutover_identity.py` preflight 回傳的 `event_head`/`event_hash`。
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
docker exec bfx-postgres psql -U bfx -d bfx -Atc \
  "SELECT count(*) FROM event_log
     WHERE event_type='RESERVATION_INTENT'
       AND occurred_at_ms >= (EXTRACT(EPOCH FROM now())*1000)::bigint - 20*60*1000"
```

### 4. 只套用 additive schema

此時只到 `8a1b2c3d4e5f`；不要先跑 `upgrade head`，因為 head 會進入 contract
gate，而 contract 必須等 application backfill verify 後才可執行：

```bash
cd ~/bfx/backend_py
uv run alembic upgrade 8a1b2c3d4e5f
uv run alembic current
```

### 5. Dry-run → apply → verify identity data cutover

使用已 review 的實際 manifest。KEK 從受控 secret store 注入
`BFX_VAULT_KEK`；stdout、JSON report 與 shell history 都不得出現 secret。先
dry-run，review aggregate counts/hash 後才 apply；apply 可安全重跑，但每次都要
保留 report：

```bash
cd ~/bfx/backend_py
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
cd ~/bfx/backend_py
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

成功結果包括 UUID `NOT NULL`、`exchange_accounts` 的 `ON DELETE RESTRICT` FK、
以 UUID 重建的 primary/unique indexes，以及只在 zero-row gate 後刪除三個 legacy
scaffold tables。contract revision 是 forward-only。

### 6a. 套用 webapi least-privilege grants

contract migration 後，以 database owner/superuser 執行一次性 grant。webapi 只
能讀 projection、account/membership，並管理自己的 credential/config draft；不得
寫入 event store、position、offer 或其他 execution tables。不要把這些 grant 放進
Alembic，避免 role 不存在時讓 migration 失敗：

```sql
GRANT USAGE ON SCHEMA public TO bfx_webapi;
GRANT SELECT ON TABLE
  public.user_profiles,
  public.exchange_accounts,
  public.exchange_account_memberships,
  public.position_state,
  public.offer_claims,
  public.event_log,
  public.attribution_weekly,
  public.funding_candles
TO bfx_webapi;
GRANT INSERT ON TABLE public.user_profiles TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE ON TABLE public.exchange_account_credentials TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.account_config_drafts TO bfx_webapi;

-- Run as the role owner and retain the output as release evidence.
SET ROLE bfx_webapi;
SELECT has_table_privilege(current_user, 'public.position_state', 'SELECT') AS can_read_position,
       has_table_privilege(current_user, 'public.event_log', 'INSERT') AS can_write_event,
       has_table_privilege(current_user, 'public.exchange_account_credentials', 'UPDATE') AS can_update_credentials;
RESET ROLE;
```

預期結果為 `can_read_position=t`、`can_write_event=f`、
`can_update_credentials=t`；任何其他 privilege 都停止 release，先修正 role。

### 7. Replay、auth boundary 與一帳號 canary gate

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
