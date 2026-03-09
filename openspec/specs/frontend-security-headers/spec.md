## ADDED Requirements

### Requirement: CSP header
系統 SHALL 在所有頁面回應中注入 `Content-Security-Policy` header，限制資源載入來源。

#### Scenario: 正常頁面載入
- **WHEN** 使用者請求任何頁面
- **THEN** 回應包含 CSP header，允許 `'self'` 來源 + Sentry 連線

#### Scenario: 阻擋外部腳本
- **WHEN** 頁面嘗試載入未授權的外部腳本
- **THEN** 瀏覽器依 CSP 策略阻擋該請求

### Requirement: 標準安全標頭
系統 SHALL 在所有頁面回應中注入以下安全標頭：
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`

#### Scenario: 防止 iframe 嵌入
- **WHEN** 第三方網站嘗試用 iframe 嵌入本站頁面
- **THEN** 瀏覽器阻擋嵌入（X-Frame-Options: DENY）

#### Scenario: API proxy 路徑排除
- **WHEN** 請求路徑為 `/api/` 開頭
- **THEN** 不注入安全標頭（由 API route handler 自行控制）

### Requirement: HSTS header
系統 SHALL 注入 `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`。

#### Scenario: HTTPS 強制
- **WHEN** 使用者首次透過 HTTPS 存取
- **THEN** 瀏覽器記錄 HSTS 策略，後續自動使用 HTTPS
