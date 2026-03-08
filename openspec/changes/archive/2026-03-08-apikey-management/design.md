## Context

用戶認證系統已完成（JWT RS256）。下一步是讓用戶綁定 Bitfinex API Key，這是放貸引擎運作的必要條件。API Key 包含 `api_key` 和 `api_secret`，是高敏感憑證，必須加密儲存。

目前已有的基礎：users table、UserRepository、fx DI、JWT middleware 保護路由。

## Goals / Non-Goals

**Goals:**
- 用戶可以安全地新增、查詢、刪除 Bitfinex API Key
- API Key secret 以 AES-256-GCM 加密儲存在 PostgreSQL
- 每個用戶最多一把 API Key（一對一關係，簡化 MVP）
- API 回應不回傳明文 secret，只回傳 masked 版本

**Non-Goals:**
- Bitfinex API 權限即時驗證（需要 bitfinex SDK 整合，留到下一步）
- API Key 輪替 / 更新（先刪後建即可）
- 多把 API Key per user（未來擴充）

## Decisions

### 1. 加密演算法：AES-256-GCM

**選擇**：AES-256-GCM via Go 標準庫 `crypto/aes` + `crypto/cipher`
**替代方案**：NaCl/secretbox、AWS KMS

AES-256-GCM 提供 authenticated encryption（同時保證機密性和完整性）。Go 標準庫直接支援，無需第三方依賴。架構文件已指定此方案。每次加密使用隨機 nonce，密文格式為 `nonce || ciphertext`。

### 2. 加密金鑰管理

AES key 透過環境變數 `AES_KEY` 注入（hex encoded 32 bytes）。在 `appconfig` 中載入並解碼為 `[]byte`。`crypto.NewAES` 接收 key bytes 建立 cipher。

### 3. DB Schema 設計

```sql
CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    api_key     TEXT NOT NULL,          -- Bitfinex API key (明文，非敏感)
    api_secret  BYTEA NOT NULL,         -- AES-256-GCM 加密後的 secret
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- `user_id` UNIQUE 確保一對一
- `api_key` 明文儲存（公開資訊，可用於 API 呼叫識別）
- `api_secret` 以 BYTEA 儲存加密後的 bytes（nonce + ciphertext）
- `ON DELETE CASCADE` 確保用戶刪除時連帶清除 key

### 4. API 回應的 Secret Masking

API 回應中 `api_secret` 欄位永遠顯示為 masked 形式（如 `"****"`），前端無法取回明文 secret。只在放貸引擎內部解密使用。

### 5. 每個用戶一把 Key

MVP 階段簡化設計。`user_id` UNIQUE constraint 自然限制。用戶更換 key 需先刪除再新增。

## Risks / Trade-offs

- **[Risk] AES_KEY 洩漏導致所有 secret 可解密** → Key 只存在環境變數，不進 git。生產環境透過 Koyeb secret 管理。未來可考慮 key rotation 機制
- **[Risk] 用戶刪除 API Key 後放貸引擎仍在運行** → 本次不處理 worker 停止邏輯（Non-Goal），後續整合時由 APIKeyService 通知 WorkerManager
- **[Risk] 明文 api_key 在 DB 中可被讀取** → api_key 不是 secret（類似 username），Bitfinex 也要求 key + secret 同時使用才有效
