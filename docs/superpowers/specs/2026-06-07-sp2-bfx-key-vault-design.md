# SP2 — BFX-key KMS vault + Bitfinex verify — Design Spec

- **日期**：2026-06-07
- **類型**：子專案 spec（epic `2026-06-06-frontend-saas-architecture-design.md` 的 SP2）
- **狀態**：DRAFT，待 Will review
- **作者**：brainstorming（subagent-driven 探索）
- **依賴**：SP1（done — self-hosted Better Auth + JWKS-verified web-API + `user_profile` 表）
- **真錢風險**：無（純新表 + 新 endpoint，不碰 daemon / canary / credentials-in-execution）
- **下一步**：通過後 → writing-plans

---

## 0. 範圍

實作 epic spec 第 159 / 187 行的 SP2：使用者的 Bitfinex API key 管理（CRUD + verify），明文 secret 經 **Server Action → server 端 KMS envelope 加密**（絕不走 client proxy hop），vault row FK `user_profile.user_id`。

**v1 = operator console，單用戶（Will = user #1）**，但 schema / 授權邊界從一開始就 per-user，為 SP6 多租戶免費鋪路。

**不在 scope**：多租戶執行（SP6，延到 G3 gate）、用 key 真正放貸（SP6）、key rotation UI（v1 = 刪除後重建）、cloud KMS（未來可平滑升級，見 §3）。

---

## 1. 設計決策（brainstorming 拍板）

| # | 決策 | 選定 | 理由 |
|---|------|------|------|
| D1 | at-rest 加密方案 | **App-managed envelope**（env KEK + per-record DEK + AES-256-GCM + `key_version`） | 與 D8 自托 Better Auth 的 portability/零鎖定立場一致；無 cloud 依賴；crypto 單一歸宿（FastAPI）；KEK 與 Better Auth 簽章 key 同風險等級（epic 風險表已接受）；未來可用 cloud KMS wrap 這把 KEK，零資料遷移 |
| D2 | Bitfinex verify 程度 | **Permission-aware fail-closed**（Funding write=on 且 Withdraw=off 才 verified） | 對托管用戶 key 的 custody-sensitive 產品，verify 該是真正安全 gate，直接執行「拿你 key 但無法提款」承諾 |
| D3 | key 基數 | **單一 key/user**（unique index on user_id） | 一人一 Bitfinex 帳號 = 自然模型；FE 也只顯示一把；取代 = 先 DELETE 再 POST；v1 多 key = YAGNI |
| D4 | 加密範圍 | **只加密 secret**；`api_key` 存明文 | api_key 是識別符、無 secret 無法簽名；標準做法；secret read 永遠 masked |
| D5 | crypto 歸宿 | **FastAPI web-API**（KEK 在其 env） | v1 唯一解密路徑 = verify；SP6 執行層也在 Python 解密，單一 crypto 家 |
| D6 | schema | **改造 dormant `APIKey` model**（FK 改 `user_profile.user_id`，envelope 欄位） | dormant 表從未進 migration → 乾淨 CREATE TABLE；保留 FE `/api-keys` 契約 |

---

## 2. 架構與資料流

明文 secret 為**單向流（只進不出）**：

```
瀏覽器（使用者貼上 api_key + api_secret）
  │  react-hook-form + Zod
  ▼
Server Action  createApiKeyAction()              ← Next server，非 client proxy
  │  server-mint 短效 EdDSA JWT（同 /api/proxy 的 mint 邏輯）
  ▼  server→server fetch ${API_URL}/api/v1/api-keys   ← 明文唯一 hop
FastAPI web-API  POST /api/v1/api-keys
  │  require_user（JWKS 驗證）→ JIT provision user_profile
  │  core/crypto.encrypt_secret()（KEK 只在 web-API env）
  ▼
Neon  api_keys（secret 密文 + wrapped DEK；status=unverified）
```

**不變式**：明文 secret 絕不 — 存明文、回 API response、進 client/Query cache、走 `/api/proxy/[...path]` 通用路徑、寫 log。

**list / delete / verify 無明文輸入** → 維持現有 `apiClient` + client proxy 路徑（FE 零改動）。

**v1 唯一解密路徑** = verify（需明文 secret 簽 Bitfinex 請求）。verify 後 secret 持續加密休眠，直到 SP6。

---

## 3. 加密模組 `core/crypto.py`（新）

App-managed envelope encryption，用 `cryptography` 套件的 `AESGCM`。

- **KEK**（Key-Encryption-Key）：`Settings.bfx_vault_kek`，base64-encoded 32 bytes，存 web-API env。缺 → 相關 endpoint 回 `503 vault_not_configured`（fail-loud，比照 `core/auth.py` 的 `auth_not_configured`）。
- **per-record DEK**（Data-Encryption-Key）：每筆隨機 32 bytes。
  - secret 密文：`AESGCM(DEK).encrypt(secret_nonce, plaintext, aad)` → `secret_ciphertext` + `secret_nonce`(12 bytes)
  - DEK wrap：`AESGCM(KEK).encrypt(dek_nonce, DEK, aad)` → `wrapped_dek` + `dek_nonce`(12 bytes)
- **AAD（associated data）**：綁 `user_id`（防 row 被搬到別的 user）。
- **`key_version`**：記用哪版 KEK wrap。未來 KEK rotation 只需重 wrap DEK、不碰 secret 密文。

API（純函式、無 I/O）：

```python
@dataclass(frozen=True)
class Envelope:
    secret_ciphertext: bytes
    secret_nonce: bytes
    wrapped_dek: bytes
    dek_nonce: bytes
    key_version: int

def encrypt_secret(plaintext: str, *, user_id: str) -> Envelope: ...
def decrypt_secret(env: Envelope, *, user_id: str) -> str: ...
```

KEK 載入 / 版本對應集中在模組內（v1 單版；多版時 `key_version -> kek_bytes` 映射）。

**未來 cloud KMS 升級路徑**：把「KEK 載入」換成「向 cloud KMS 請求 unwrap DEK」即可，envelope 欄位不變、資料零遷移。

---

## 4. Vault schema（改造 dormant `APIKey`，手刻 migration）

`api_keys` 表從未進任何 migration（Go-era dormant model，live Neon 無此表）→ 乾淨 CREATE TABLE。

| 欄 | 型別 | 約束 / 預設 | 說明 |
|----|------|------------|------|
| `id` | UUID | PK, `gen_random_uuid()` | |
| `user_id` | **TEXT** | FK `user_profile.user_id` ON DELETE CASCADE, NOT NULL | 直接 = JWT `principal.user_id`，verify 免 join user_profile |
| `label` | TEXT | NOT NULL, default `''` | |
| `api_key` | TEXT | NOT NULL | 明文識別符 |
| `secret_ciphertext` | BYTEA | NOT NULL | |
| `secret_nonce` | BYTEA | NOT NULL | |
| `wrapped_dek` | BYTEA | NOT NULL | |
| `dek_nonce` | BYTEA | NOT NULL | |
| `key_version` | INTEGER | NOT NULL, default 1 | |
| `exchange_status` | TEXT | NOT NULL, default `'unverified'` | `unverified` / `verified` / `failed` |
| `verified_at` | TIMESTAMPTZ | NULL | |
| `last_verify_error` | TEXT | NULL | 失敗原因 code |
| `created_at` | TIMESTAMPTZ | NOT NULL, `now()` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, `now()` | |

- **`UNIQUE INDEX idx_api_keys_user_id ON (user_id)`**（單一 key/user）。
- model FK 從 dormant `users.id` 改為 `user_profile.user_id`；移除舊 `api_secret LargeBinary` 單欄，換成上述 envelope 欄位。
- 不動其他 Go-era model（`User` / `UserConfig` 等），非本 scope。

**Migration 慣例**（與 SP1 一致）：
- migration 檔**手刻純 DDL**（`.env` = live Neon，不可 `alembic upgrade` / `--autogenerate` / `check` 對 live 跑）。
- 本機對 **testcontainers Postgres** 測 upgrade/downgrade。
- live 套用 = VM `migrate` compose service 跑 `alembic upgrade head`（deploy-vm.sh，與 SP1 migration e61f3d1ed7ca 同路徑）。
- **`GRANT` 給 `bfx_webapi` 走 psql runbook**，不進 migration（role 是 out-of-band 建的，GRANT 進 migration 會在 local/test 無此 role 時炸）。

---

## 5. Bitfinex verify

新增 `BitfinexAuthREST.get_key_permissions(creds) -> KeyPermissions`：
- 打 authenticated `POST /v2/auth/r/permissions`，複用現有 `sign_request()`（HMAC-SHA384，`external/bitfinex/auth_ws.py:296`）。
- 回傳解析成 scope→{read, write} 的結構（Bitfinex 回 `[[scope, read, write], ...]`）。

**fail-closed 判定**（`verify` endpoint 內）：

| 條件 | 結果 |
|------|------|
| 簽章被拒 / 401 | `failed`, `last_verify_error = "invalid_credentials"` |
| `funding` write != 1 | `failed`, `funding_write_required` |
| `withdraw` write == 1（或 scope present 且 enabled） | `failed`, `withdraw_must_be_disabled` |
| funding write=on 且 withdraw off | `verified`, `verified_at = now()`, `last_verify_error = NULL` |
| 網路 / 上游 5xx | endpoint 回 502；status 不變（不誤標 failed）|

流程：load row（scope by user_id）→ `decrypt_secret` → 構 ephemeral credentials（`execution/protocols.py` 的 dataclass）→ `get_key_permissions` → 判定 → UPDATE status/verified_at/last_verify_error → 回 `VerifyResult`。

---

## 6. Web-API endpoints

新增 router（`modules/api/` 下新檔，或擴 `routers.py`），全部 `Depends(require_user)`、scope by `principal.user_id`、`{data}` envelope：

| Method | Path | 行為 | 錯誤 |
|--------|------|------|------|
| GET | `/api/v1/api-keys` | list 當前 user 的 key（secret 永遠 masked `"****"`）| — |
| POST | `/api/v1/api-keys` | JIT provision user_profile → encrypt → insert（status=unverified）→ 回 masked DTO | 已有 key → 409 `key_already_exists`；body 缺 → 422 |
| POST | `/api/v1/api-keys/{id}/verify` | §5 流程 | id 非當前 user → 404；KEK 缺 → 503 |
| DELETE | `/api/v1/api-keys/{id}` | 刪除 | id 非當前 user → 404 |

- **跨用戶存取一律回 404**（非 403，避免 resource enumeration）。
- DB session：用 `core/db.py` 的 `async_sessionmaker` / `session_scope`（為 router 加一個 `get_session` FastAPI dependency；SP2 是首個碰 DB 的 web-API endpoint）。
- **JIT provision**：create 時若 `user_profile` 無此 `user_id` row → 先 insert（`plan='free'`；`user_profile` 無 role 欄，role 一律來自 JWT claim、不落 DB）。集中成一個 helper（SP3+ 共用）。

**Pydantic schema**（`modules/api/schemas.py` 或 feature-local）—— **response key 一律 camelCase**，對齊 SP1 profile endpoint（回 `userId`/`email`/`role`）與 FE `frontend/src/types/index.ts`：
- `CreateApiKeyRequest{label, apiKey, apiSecret}`（FE hook 送 camelCase；用 Pydantic `alias` 或 `populate_by_name` 接）
- `ApiKeyResponse{id, label, apiKey, apiSecret="****", exchangeStatus, createdAt, verifiedAt}`
- `VerifyResultResponse{status, error?}`

對齊 FE `ApiKey` / `VerifyResult` DTO，避免 §10 的契約漂移。

---

## 7. Frontend 改動（最小）

| 檔 | 改動 |
|----|------|
| `src/app/[locale]/(dashboard)/api-keys/actions.ts`（新） | `createApiKeyAction(input)` Server Action：取 session → server-mint JWT（同 proxy 邏輯）→ `fetch(${API_URL}/api/v1/api-keys)` server-side → 回結果。比照 `(auth)/actions.ts` |
| `src/features/api-keys/hooks/use-api-keys.ts` | `useCreateApiKey.mutationFn` 從 `apiClient.post("/api-keys")` 改呼 `createApiKeyAction`；`onSuccess` invalidate 不變 |
| 其餘（list/delete/verify hooks、DTO、components）| 不動 |

明文 api_secret 因此只在 Server Action 的 server-side fetch body 出現，不經 client `apiClient` / `/api/proxy`。

---

## 8. 測試（TDD，`cd backend_py && uv run pytest -m "not integration"` 全綠才 commit）

- **crypto** (`tests/.../test_crypto.py`)：encrypt→decrypt round-trip；tamper ciphertext→`InvalidTag`；wrong KEK→fail；wrong user_id AAD→fail；nonce 每次唯一。
- **verify**（respx / httpx mock Bitfinex）：permission matrix 解析；funding-on+withdraw-off→verified；withdraw-on→failed `withdraw_must_be_disabled`；funding-off→failed；bad signature→failed `invalid_credentials`；上游 5xx→502。
- **router**（testcontainers PG）：scope 隔離（別人 id→404）；JIT provision 建 profile；list masked；create→verify→list happy path；duplicate→409；KEK 缺→503。
- **migration**（testcontainers PG）：upgrade 建表 + unique index + FK；downgrade 乾淨還原。

---

## 9. 部署增量

- **`scripts/deploy-vm.sh`**：webapi.env 組裝加 `BFX_VAULT_KEK`（比照 SP1 加 `DATABASE_URL` / `BETTER_AUTH_JWKS_URL`）。
- **`docker-compose.bot.yml`**：webapi service `env_file: .env.webapi.runtime` 已涵蓋，無需改 compose。
- **Runbook（手動，Will 有 prod 存取時）**：
  1. 產 KEK：`openssl rand -base64 32` → 存 `~/bfx/webapi.env` 的 `BFX_VAULT_KEK=`，並複製進 `~/second-brain/secrets/`。
  2. psql（superuser）：`GRANT SELECT, INSERT, UPDATE, DELETE ON api_keys TO bfx_webapi;`
  3. 本機改 source → VM 跑 `scripts/deploy-vm.sh canary`（git pull + build + migrate 套表 + recreate webapi）。**絕不手改 VM。**
  4. Vercel：無新 env（Server Action 用既有 `API_URL`）。
  5. smoke：FE 新增 key → verify → 綠勾；DB 確認 secret 為密文、`exchange_status=verified`。

---

## 10. 風險與緩解

| 風險 | 嚴重度 | 緩解 |
|------|--------|------|
| KEK 外洩 = 所有 secret 可解 | 中 | KEK 當 crown jewel（同 Better Auth 簽章 key）；env 管理、second-brain secrets 備份；`key_version` 支援未來 rotation；未來可升 cloud KMS |
| verify 把合法 key 誤判 failed（permission 解析錯）| 中 | permission matrix 解析有專測；fail 時存明確 `last_verify_error` 供 debug；用戶可重 verify |
| FE↔BE DTO 漂移（camelCase / masked 欄位）| 低 | response schema 對齊 `types/index.ts`；沿用 SP1 命名慣例；型別測試 |
| 忘記 GRANT → webapi 無權限存取新表 | 低 | runbook 明列；首次 smoke 立即暴露 |
| 明文 secret 不慎入 log | 中 | 不變式明列；crypto 模組不接受 logger；router 不 log body |
