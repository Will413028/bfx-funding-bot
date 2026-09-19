# Release 0 operator-only containment runbook

本 runbook 是上線前的人工 gate，目標是讓 Release 0 只剩一個明確的
Better Auth operator 可以建立 execution-capable session。工具預設只讀；本
專案未在本次開發中連線或修改任何 production DB、Redis、session。

## Invariants

- `BFX_OPERATOR_USER_ID` 是 Better Auth `auth."user".id`，不是 email、profile
  UUID 或 Bitfinex account ID。
- Frontend 與 backend 必須使用同一個 non-empty ID；backend
  `BFX_OPERATOR_ROLE` 必須是 `admin`。
- operator 必須已完成 TOTP enrollment；非 operator 帳號會被永久標記為
  `banned=true`，並撤銷其 Better Auth secondary-storage session 與
  `bfx:mfa-verified:<session-token>` marker。
- 公開 proof/funding endpoints 只走 exact public allowlist；其他 proxy path
  預設都是 private，必須通過 operator identity、fresh session lookup 與
  per-session MFA marker。

## 0. Planned halt and preflight

1. 宣布 maintenance window，暫停 daemon submit/write path；保留 `/health` 與
   `/ready` 供 deployment probe 使用。不要在 halt 期間進行 schema 或 auth
   手工修改。
2. 確認 deploy artifact/branch SHA，並在 release evidence 記錄：

   ```bash
   git rev-parse HEAD
   cd backend_py && uv run alembic check
   ```

3. 以 dev/staging database 先套用 migration，再確認 production migration
   policy；正式套用一律使用：

   ```bash
   cd backend_py && uv run alembic upgrade head
   ```

4. 由受控管理流程建立唯一 operator user，設定 `role=admin`，完成並驗證
   TOTP。把 user ID（不記錄 password、TOTP secret、JWT 或 connection URL）
   寫入受控 secret store。
5. 確認 `~/bfx/webapi.env` 與 `~/bfx/frontend.env` 都有：

   ```dotenv
   BFX_OPERATOR_USER_ID=<same Better Auth user id>
   BFX_OPERATOR_ROLE=admin
   ```

   現行 [immutable release](immutable-release.md) 工具在啟動前拒絕缺值、非 admin role 或 ID
   不一致。

## 1. Inventory first (read-only)

在 planned halt 中、stack host 的 repository root 執行。Revocation tool 已隨
frontend standalone image 放在 `/app/scripts`，`pg`/`ioredis` 也由 image 的
server dependency trace 提供；Compose `env_file` 會注入 frontend 的 DB、Redis
與 operator 設定。先建立只有 release operator 可讀的 evidence 目錄，再把
audit path 指向新的、尚未存在的檔案（工具使用 `O_EXCL`，不會覆蓋舊 evidence）：

```bash
cd ~/bfx
mkdir -p release-evidence
chmod 700 release-evidence
docker compose --env-file .env.frontend.runtime -f docker-compose.bot.yml run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/release-evidence:/evidence" \
  frontend node /app/scripts/revoke-non-operators.mjs \
  --dry-run \
  --audit-file /evidence/containment-dry-run-$(date +%s).json
```

工具會 fail closed，並驗證：

- database 中 operator row 存在、role 是 `admin`、未被 banned、TOTP 已啟用；
- 所有其他 `auth."user"` IDs；
- 每個非 operator 的 `active-sessions-<user-id>` inventory 可解析；
- dry-run 沒有 `UPDATE`、Redis `DEL` 或其他寫入。

檢查 stdout/audit JSON 的 `operatorUserId`、`nonOperatorCount`、
`activeSessionCount`。若 operator ID、數量或 session inventory 不符合預期，
停止並修正資料，不得直接 apply。

## 2. Apply (explicit, audited)

只有在 planned halt、backup/evidence 與 dry-run review 都完成後才可執行。這個
命令要求兩個旗標，避免把 apply 當成一般 dry-run：

```bash
cd ~/bfx
docker compose --env-file .env.frontend.runtime -f docker-compose.bot.yml run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/release-evidence:/evidence" \
  frontend node /app/scripts/revoke-non-operators.mjs \
  --apply \
  --confirm-release-0 \
  --audit-file /evidence/containment-apply-$(date +%s).json
```

執行順序固定為：

1. lock/validate operator row；
2. 在單一 transaction 將所有其他 user 設為 `banned=true`，寫入固定
   `banReason`；operator 不會被 update；
3. transaction commit 後，刪除非 operator active session token、session list
   與 per-session MFA marker。

工具不會 `FLUSHALL`/`FLUSHDB`，也不會刪除 user、API key、交易資料或 operator
   row。若 Redis 清理中斷，audit `status` 會是 `partial_failure`；保持 halt，
   用同一個 operator ID 重跑 apply（操作具 idempotency），直到得到
   `status=completed`。

## 3. Evidence and direct boundary checks

保存以下不含 secrets 的 evidence：apply audit JSON、release SHA、migration
結果、部署 preflight stdout，以及測試結果。Release gate 至少包含：

```bash
cd ~/bfx/frontend
pnpm test
pnpm lint
pnpm build
E2E_FULL_STACK=1 pnpm test:e2e --project=chromium

cd ../backend_py
uv run pytest -m "not integration" -q
uv run mypy src/
uv run ruff check
```

Full-stack Playwright 必須直接確認：

- `POST /api/auth/sign-up/email` 回 `403 signup_disabled`；
- `GET /api/auth/token`、`POST /api/auth/sign-jwt`、`POST /api/auth/verify-jwt`
  都回 `404`；
- `GET /api/auth/get-session` response 不含 `set-auth-jwt`；
- 未完成 MFA 的 private proxy request 在呼叫 `getToken`/backend 前回
  `403 mfa_required`；
- exact public proof/funding path 可匿名讀取，path traversal 與其他 private
  path 仍被拒絕。

## 4. Rollback / rotation

- 不要用全域 `banned=false` 作為 rollback。若誤選 operator，維持 planned halt，
  由兩人 review 後用 targeted backup/forward-fix 恢復正確 user row，並重新撤銷
  受影響 session；所有決定寫入 incident evidence。
- 若要 rotation，先建立並完成新 operator 的 TOTP，再同步兩邊 env、重新執行
  dry-run/apply；舊 operator 會被視為 non-operator 並撤銷 session。
- 若 apply 在 DB commit 後失敗，不能回復 authorization policy 來「繞過」問題；
  修復 Redis connectivity 後重跑同一工具。

## 5. Scope boundary

Release 0 只保證 operator-only containment、signup 關閉、session/MFA/JWT
邊界與 deployment readiness。它不宣稱 Halt 1 membership/account isolation；
多使用者 SaaS、account membership、billing 與 tenant isolation 仍由後續
architecture plan 處理。
