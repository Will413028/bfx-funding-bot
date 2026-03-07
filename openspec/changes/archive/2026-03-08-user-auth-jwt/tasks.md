## 1. Dependencies & Config

- [x] 1.1 新增 Go 依賴：`golang-jwt/jwt/v5`、`golang.org/x/crypto`（bcrypt）
- [x] 1.2 擴充 `internal/appconfig/config.go`：新增 `JWTPrivateKey`、`JWTPublicKey` 欄位，啟動時從環境變數載入 PEM 並解析為 `*rsa.PrivateKey` / `*rsa.PublicKey`
- [x] 1.3 建立 `.env.example` 中的 JWT key pair placeholder（開發用），並產生開發用 key pair 說明

## 2. Auth Infrastructure

- [x] 2.1 建立 `internal/auth/jwt.go`：`JWTManager` struct，提供 `GenerateToken(userID, email) (string, time.Time, error)` 和 `ValidateToken(tokenString) (*Claims, error)` 方法
- [x] 2.2 以 fx.Provide 註冊 `auth.NewJWTManager`，注入 appconfig 中的 RSA key pair

## 3. Service Layer

- [x] 3.1 建立 `internal/service/user.go`：`UserService` struct，注入 `UserRepository` + `auth.JWTManager` + bcrypt
- [x] 3.2 實作 `Register(ctx, email, password) (*domain.User, error)`：email 格式驗證、密碼長度驗證、bcrypt hash、呼叫 repo.Create、處理 duplicate email 錯誤
- [x] 3.3 實作 `Login(ctx, email, password) (token string, expiresAt time.Time, error)`：查詢用戶、檢查 status、bcrypt 比對、簽發 JWT

## 4. Domain Layer

- [x] 4.1 擴充 `internal/domain/errors.go`：新增 auth 相關 domain error（`ErrEmailAlreadyExists`、`ErrInvalidCredentials`、`ErrUserSuspended`、`ErrInvalidEmail`、`ErrPasswordTooShort`）

## 5. Transport Layer

- [x] 5.1 建立 `internal/handler/auth.go`：`AuthHandler` struct，注入 `UserService`，提供 `Register` 和 `Login` handler 方法
- [x] 5.2 實作 `Register` handler：解析 request body、呼叫 service、回傳 201 或錯誤
- [x] 5.3 實作 `Login` handler：解析 request body、呼叫 service、回傳 200 + token 或錯誤
- [x] 5.4 建立 `internal/middleware/jwt.go`：JWT 驗證 middleware，從 Authorization header 取得 Bearer token、驗證、將 userID/email 寫入 gin.Context
- [x] 5.5 修改 `internal/handler/router.go`：新增 auth 路由 group（public），對其餘 v1 路由套用 JWT middleware

## 6. DI Wiring

- [x] 6.1 修改 `cmd/server/main.go`：fx.Provide 新增 `auth.NewJWTManager`、`service.NewUserService`、`handler.NewAuthHandler`，並將 `JWTManager` 傳入 router

## 7. Testing

- [x] 7.1 為 `auth.JWTManager` 撰寫 unit test：token 生成、驗證、過期、篡改偵測
- [x] 7.2 為 `service.UserService` 撰寫 unit test：註冊成功、duplicate email、登入成功、密碼錯誤、suspended user
- [x] 7.3 為 JWT middleware 撰寫 unit test：valid token、missing token、invalid token、expired token
