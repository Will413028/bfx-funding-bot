## Context

架構文件定義 `notification/` 為獨立 package（不在 `infra/` 內），有自己的業務邏輯。`service/` 層透過 interface 呼叫通知功能。

## Goals / Non-Goals

**Goals:**
- `Notifier` interface：定義通知契約，service 層依賴此 interface
- Resend 實作：使用 Resend Go SDK 發送 Email
- 支援通知類型：Welcome、APIKeyAlert、GeneralAlert
- fx 自動注入

**Non-Goals:**
- 通知偏好設定（用戶選擇接收哪些通知）
- 通知歷史紀錄持久化
- Telegram / Slack 等其他管道
- Email 模板引擎（先用純文字）

## Decisions

### 1. Consumer-side interface

`notification/` package 自己暴露 `Notifier` interface。`service/` 層依賴此 interface，Resend 實作透過 fx 注入。

```go
type Notifier interface {
    SendWelcome(ctx context.Context, email string) error
    SendAPIKeyAlert(ctx context.Context, email, message string) error
    SendAlert(ctx context.Context, email, subject, message string) error
}
```

### 2. Resend SDK

使用官方 `resend-go/v2`，API 簡潔。免費額度 100 emails/day，初期足夠。

### 3. 非同步 fire-and-forget

通知失敗不應阻塞主要業務流程。呼叫方 log error 即可，不需要 retry 機制。

## Risks / Trade-offs

- **[風險] Resend API 故障** → 通知丟失但不影響放貸引擎運作。log 記錄失敗的通知，未來可加 retry queue。
- **[取捨] 純文字 vs HTML 模板** → 初期用純文字，簡單快速。未來需求增加時再引入模板引擎。
