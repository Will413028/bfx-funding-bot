# Release 0 operator-only containment runbook

本 runbook 是上線前的人工 gate，目標是讓 Release 0 只剩一個明確的
Better Auth operator 可以建立 execution-capable session。工具預設只讀；本
專案未在本次開發中連線或修改任何 production DB、Redis、session。

應用程式的部署一律走 [deploy runbook](deploy.md)（CI → GHCR → `bfx-deploy`）；本文件只描述
operator bootstrap 與 non-operator containment 這兩個 one-shot，以及它們的 auth 邊界檢查。

## Invariants

- `BFX_OPERATOR_USER_ID` 是 Better Auth `auth."user".id`，不是 email、profile
  UUID 或 Bitfinex account ID。
- Frontend 與 backend 必須使用同一個 non-empty ID；backend
  `BFX_OPERATOR_ROLE` 必須是 `admin`。
- operator 必須先以實際 Better Auth 流程完成並驗證 TOTP，再由 audited
  first-admin bootstrap 指派唯一 `admin` role；不得手改任何 enrollment flag。
  非 operator 帳號會被永久標記為
  `banned=true`，並撤銷其 Better Auth secondary-storage session 與
  `bfx:mfa-verified:<session-token>` marker。
- 公開 proof/funding endpoints 只走 exact public allowlist；其他 proxy path
  預設都是 private，必須通過 operator identity、fresh session lookup 與
  per-session MFA marker。

## 0. Planned halt and preflight

1. 宣布 maintenance window，交易狀態維持 `HALTED` 或 `REDUCING`（見
   [operations runbook](operations.md)）；保留 health probe。不要在停機期間進行 schema
   或 auth 手工修改。
2. 從 `deployments` ledger 記錄目前部署的 source revision 與 frontend/backend digest
   （查詢見 deploy runbook）。
3. Schema 只由 bfx-deploy 在部署時以 `alembic upgrade head` 套用（先備份）；不要在 VM 上
   手動跑 migration。本機驗證仍用：

   ```bash
   cd backend && uv run alembic check
   ```

4. 由既有受控管理流程建立唯一 operator user，但先維持非 admin role。以該
   exact configured user 的 session 進入 `/{locale}/settings/security`，使用實際
   Better Auth enrollment 完成 TOTP 驗證與 fresh-session proof。不得手改
   `role`、`twoFactorEnabled`、`twoFactor.verified`、email verification 或 Redis
   MFA marker。把 user ID（不記錄 password、TOTP secret、backup code、JWT 或
   connection URL）寫入受控 secret store。
5. 確認 protected `/opt/bfx/runtime/webapi.env` 與
   `/opt/bfx/runtime/frontend.env` 都有：

   ```dotenv
   BFX_OPERATOR_USER_ID=<same Better Auth user id>
   BFX_OPERATOR_ROLE=admin
   ```

   `bfx-deploy` 只檢查 env 檔格式與權限；缺值、非 admin role 或 ID 不一致由 webapi 與
   frontend 在執行時 fail closed（所有 private route 拒絕），所以部署後必須用 fresh sign-in
   確認。

## 1. Approved-image one-shot boundary

現行 frontend 是 read-only rootfs，且沒有 audit/tmp mount；**不得 `docker exec`
進 runtime container 寫 evidence**。Legacy Compose app/run profile（`docker-compose.bot.yml`
的 `legacy-app`）也不是部署路徑。Bootstrap 與後續 containment 都必須是 stack host 上的獨立
one-shot，使用目前部署的 frontend image（`deployments` ledger 最新 `deployed` 列的
`frontend_digest`，或 `docker inspect bfx-frontend --format '{{.Image}}'`）、現行 protected
`/opt/bfx/runtime/frontend.env`、既有 `bfx_default` network，以及 root-owned mode `0700`
的 private audit bind。

以下是 controller 必須落實的 manual one-shot contract；`COMMAND...` 每次只換成
下節列出的單一命令，`AUDIT_DIR`／container name／audit filename 每次皆為新的：

```bash
ACTUAL_FRONTEND_IMAGE=sha256:<docker inspect bfx-frontend 的 Image>
AUDIT_DIR=/opt/bfx/operator-audit/<new-run-id>
CONTAINER_NAME=bfx-operator-one-shot-<new-run-id>
install -d -o root -g root -m 0700 "$AUDIT_DIR"

docker create --pull=never --read-only --user 0:0 --entrypoint "" \
  --workdir /app --name "$CONTAINER_NAME" --network bfx_default \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --env-file /opt/bfx/runtime/frontend.env \
  --mount "type=bind,src=$AUDIT_DIR,dst=/evidence" \
  "$ACTUAL_FRONTEND_IMAGE" COMMAND...
```

這不是可直接略過 inspection 的 `docker run`。記下 create 回傳的 exact container
ID，**在任何執行前**用不輸出 env values 的受控檢查確認：container ID/Image
等於本次 create 與上面的 image ID；state=`created`；command 完全相等；
user=`0:0`、cwd=`/app`、read-only rootfs、非 privileged、cap-drop ALL、
no-new-privileges、network=`bfx_default`；env keys/values 與 protected env file
逐項相等；唯一 mount 是上述 `/evidence` writable bind，來源與目的 exact。
不符立即停止，不 start。符合才 `docker start --attach <exact-id>`，檢查 container
process exit 0；失敗不重試同一命令，保留 receipt/state 調查。完成檢查後只移除
本次已驗證的 stopped container。不得 pull/build、把 secrets 放在 command line、
dump raw inspect/env、掃描 Redis 或把 audit 寫到 container rootfs。

CLI audit 自身以 exclusive create 保留新 path 並固定 mode `0600`。one-shot 使用
root 僅為了在 root-owned private bind 建立 root-owned receipt；rootfs 仍 read-only、
capabilities 全移除，且 container 不持有 Docker socket。

## 2. Audited first-admin bootstrap

人工 enrollment 與 fresh-session proof 完成後，先用上述 one-shot contract 執行
預設 dry-run（audit filename 必須尚不存在）：

```text
node /app/scripts/bootstrap-operator.mjs --dry-run \
  --audit-file /evidence/bootstrap-dry-run.json
```

工具在 read-only transaction 中獨立驗證 DB，不信任 browser claims：configured
user 必須 exact 唯一、未 banned、`twoFactorEnabled=true`；必須有 exact 一筆
canonical credential account 與 exact 一筆 `verified=true` TOTP；不得已有其他
exact 或 comma-separated admin authority。它同時解析 operator 的 bounded
`active-sessions-<user-id>` inventory，但 dry-run 不 lock table、不 UPDATE、不 DEL。
確認 redacted JSON 的 `operatorUserId`、`dbCommitted=false`、role/session counts。

review dry-run receipt 後，以全新的 audit directory/container/path 執行 apply：

```text
node /app/scripts/bootstrap-operator.mjs --apply \
  --confirm-bootstrap-admin \
  --audit-file /evidence/bootstrap-apply.json
```

apply 先 reserve mode `0600` audit，才開始 bounded DB transaction；鎖住 authority
後只將 configured ID 的 `role` 改為 exact `admin`，不改 enrollment/auth flags。
commit 後只依 operator inventory 刪除其 list、exact session tokens 與對應
`bfx:mfa-verified:<token>`。若 COMMIT acknowledgement 遺失，receipt 會標示
`status=outcome_unknown`、unknown database outcome 與 required reconciliation；不得
把它解讀為未提交或繼續 Redis 階段。若 Redis batch acknowledgement 遺失，receipt
只記錄已確認刪除數的 lower bound、unknown batch 與 inventory 狀態；inventory 在
所有 session-key batches 確認前保留，供安全重試。以上情況或 `partial_failure` 都必須
保持 halt；核對 receipt、修復 dependency 後用新的 audit path 重跑同一 sole admin，
直到 `completed`。既有 audit path 永不覆寫。

Bootstrap 會撤銷 enrollment session。完成後瀏覽器先回 Overview，使用既有 desktop
sidebar 或 mobile menu 的 Logout 清除舊 cookie，再 fresh sign-in 並完成 TOTP，確認
新的 fresh operator session；不得以舊 cookie 或人工 Redis marker 作 proof。若撤銷後
stale cookie 令 Security page 顯示 404，不要在該頁重試或手改資料，仍依上述 Overview
Logout 路徑重新登入。

## 3. Non-operator inventory first (read-only)

first-admin bootstrap 完成且 fresh sign-in proof 通過後，existing revocation tool
仍是獨立 one-shot。以第 1 節同一 contract（新的 audit dir/container/path）執行：

```text
node /app/scripts/revoke-non-operators.mjs --dry-run \
  --audit-file /evidence/containment-dry-run.json
```

工具會 fail closed，並驗證：

- database 中 operator row 存在、role 是 `admin`、未被 banned、TOTP 已啟用；
- 所有其他 `auth."user"` IDs；
- 每個非 operator 的 `active-sessions-<user-id>` inventory 可解析；
- dry-run 沒有 `UPDATE`、Redis `DEL` 或其他寫入。

檢查 stdout/audit JSON 的 `operatorUserId`、`nonOperatorCount`、
`activeSessionCount`。若 operator ID、數量或 session inventory 不符合預期，
停止並修正資料，不得直接 apply。

## 4. Non-operator apply (explicit, audited)

只有在 planned halt、backup/evidence 與 dry-run review 都完成後才可執行。這個
命令要求兩個旗標，避免把 apply 當成一般 dry-run：

```text
node /app/scripts/revoke-non-operators.mjs --apply \
  --confirm-release-0 \
  --audit-file /evidence/containment-apply.json
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

## 5. Evidence and direct boundary checks

保存以下不含 secrets 的 evidence：apply audit JSON、部署的 source revision 與 digest
（`deployments` ledger）、以及測試結果。CI 已對每個 main commit 跑前後端測試；手動複核至少包含：

```bash
cd ~/bfx/frontend
pnpm test
pnpm lint
pnpm build
E2E_FULL_STACK=1 pnpm test:e2e --project=chromium

cd ../backend
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

## 6. Rollback / rotation

- 不要用全域 `banned=false` 作為 rollback。若誤選 operator，維持 planned halt，
  由兩人 review 後用 targeted backup/forward-fix 恢復正確 user row，並重新撤銷
  受影響 session；所有決定寫入 incident evidence。
- 若要 rotation，先建立並完成新 configured operator 的 TOTP，再同步兩邊 env、
  對新 ID 重走 audited first-admin/authority change review，最後才重新執行
  non-operator dry-run/apply；不得讓 bootstrap 繞過既有 admin conflict。
- 若 apply 在 DB commit 後失敗，不能回復 authorization policy 來「繞過」問題；
  修復 Redis connectivity 後重跑同一工具。

## 7. Scope boundary

Release 0 只保證 operator-only containment、signup 關閉、session/MFA/JWT
邊界與 deployment readiness。它不宣稱 Halt 1 membership/account isolation；
多使用者 SaaS、account membership、billing 與 tenant isolation 仍由後續
architecture plan 處理。

完成 bootstrap、fresh sign-in 與 containment 只建立 authenticated control plane；
交易狀態不變。它們不授權 submit 或 resume；恢復交易只能由 operator 在 UI 以 TOTP 執行
（見 [operations runbook](operations.md)）。
