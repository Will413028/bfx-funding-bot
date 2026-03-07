## Context

後端已具備基礎骨架：fx DI、Gin router、Neon PostgreSQL 連線、User domain model + UserRepository + sqlc 生成碼。`users` table 已在 Neon 上透過 Atlas migration 建立。目前只有 `/api/v1/health` 一個 endpoint，所有業務 API 尚未開始。

認證系統是第一個業務功能，後續 API Key 管理、策略設定、計費查詢都依賴它。

## Goals / Non-Goals

**Goals:**
- 用戶可以用 email + password 註冊帳號
- 用戶可以登入取得 JWT access token
- JWT middleware 保護所有業務 API endpoint
- 符合架構文件的分層規範（handler → service → repository）

**Non-Goals:**
- OAuth / 第三方登入（未來擴充）
- Refresh token + Redis session 管理（本次只做 access token，session 留到後續）
- Email 驗證 / 忘記密碼流程
- Rate limiting（已在架構規劃中，但不在本次範圍）

## Decisions

### 1. JWT 簽名演算法：RS256

**選擇**：RS256（RSA + SHA-256）
**替代方案**：HS256（HMAC + SHA-256）

RS256 使用非對稱 key pair，private key 簽發、public key 驗證。優勢：
- 未來多服務架構下，只需分發 public key 即可驗證 token，不需共享 secret
- 架構文件已明確指定 RS256

### 2. JWT package：golang-jwt/jwt/v5

Go 社區標準 JWT library，穩定且廣泛使用。不考慮自行實作。

### 3. 密碼雜湊：bcrypt（cost=12）

**選擇**：bcrypt via `golang.org/x/crypto/bcrypt`
**替代方案**：argon2id

bcrypt 是業界成熟方案，Go 標準擴充庫直接支援。cost=12 在現代硬體上約 250ms，安全性與效能取得平衡。

### 4. Auth 邏輯獨立為 `internal/auth/` package

JWT 簽發/驗證邏輯放在 `internal/auth/jwt.go`，而非 middleware 或 service 內。原因：
- middleware 和 service 都需要使用 JWT 邏輯，獨立 package 避免重複
- 符合架構文件 Infrastructure 層的定位
- fx 注入 `*auth.JWTManager`，middleware 和 service 各取所需

### 5. Token 有效期：24 小時

Access token 有效期 24 小時。不設 refresh token（Non-Goal）。用戶過期後重新登入。這對 MVP 階段足夠，後續加入 refresh token 時不影響現有架構。

### 6. JWT Claims 結構

```go
type Claims struct {
    jwt.RegisteredClaims
    UserID string `json:"uid"`
    Email  string `json:"email"`
}
```

Payload 只放必要資訊（UserID + Email），不放敏感資料。`sub` 欄位設為 UserID。

### 7. 環境變數載入 RS256 Key Pair

PEM 格式的 key pair 透過環境變數 `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` 注入。在 `appconfig` 中新增欄位，啟動時解析為 `*rsa.PrivateKey` / `*rsa.PublicKey`。

## Risks / Trade-offs

- **[Risk] 無 refresh token，用戶需每 24 小時重新登入** → 可接受的 MVP 限制，後續迭代加入 refresh token + Redis session
- **[Risk] 無 rate limiting，登入端點可能被暴力破解** → bcrypt 本身有計算成本做為天然防護；rate limiting 規劃在下一個 change
- **[Risk] RS256 private key 洩漏** → 透過 Koyeb 環境變數注入，不進 git。開發環境用 `.env` 檔案
