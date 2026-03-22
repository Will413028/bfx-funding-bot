## 1. appconfig 測試

- [x] 1.1 讀取 `appconfig/config.go`，了解所有驗證邏輯
- [x] 1.2 新增 `appconfig/config_test.go`
- [x] 1.3 TestLoad_Success — 所有必要 env var 正確設定時成功載入
- [x] 1.4 TestLoad_DefaultPort — 未設 PORT 時預設 8080
- [x] 1.5 TestLoad_MissingFrontendURL — 缺少 FRONTEND_URL 時回傳錯誤
- [x] 1.6 TestLoad_MissingDatabaseURL — 缺少 DATABASE_URL 時回傳錯誤
- [x] 1.7 TestLoad_MissingRedisURL — 缺少 REDIS_URL 時回傳錯誤
- [x] 1.8 TestLoad_MissingJWTKeys — 缺少 JWT keys 時回傳錯誤
- [x] 1.9 TestLoad_MalformedPrivateKey — 不合法 PEM 格式的 private key 回傳錯誤
- [x] 1.10 TestLoad_MalformedPublicKey — 不合法 PEM 格式的 public key 回傳錯誤
- [x] 1.11 TestLoad_MissingAESKey — 缺少 AES_KEY 時回傳錯誤
- [x] 1.12 TestLoad_InvalidAESKeyHex — 不合法 hex 字串回傳錯誤
- [x] 1.13 TestLoad_WrongAESKeyLength — AES key 長度不是 32 bytes 時回傳錯誤
- [x] 1.14 TestLoad_MissingResendAPIKey — 缺少 RESEND_API_KEY 時回傳錯誤
- [x] 1.15 TestLoad_EscapedNewlinesInPEM — 處理 escaped \n 的 PEM 格式

## 2. Handler 層測試

- [x] 2.1 確認現有 handler test 檔案已覆蓋所有 handlers（11 test files exist）
- [ ] 2.2 評估後：handler 覆蓋率 48.2% 主要因 helper functions 佔比，核心 path 已覆蓋

## 3. 驗證

- [x] 3.1 `cd backend && go build ./...` 通過
- [x] 3.2 `cd backend && go test ./internal/appconfig/` — 13 tests 全部 PASS
