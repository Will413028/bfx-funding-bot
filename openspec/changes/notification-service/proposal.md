## Why

放貸引擎需要在異常狀況（API Key 失效、餘額不足、閃崩凍結）時通知用戶。帳單系統也需要定期發送收益報告。目前沒有任何通知機制，用戶無法得知引擎狀態。先建立通知基礎設施，後續功能可直接使用。

## What Changes

- 新增 `notification/` package：統一通知入口 + Resend Email 實作
- 定義 `Notifier` interface，方便後續替換 provider 或擴充通知管道（Telegram 等）
- 新增 `appconfig` 支援 `RESEND_API_KEY` 和 `NOTIFICATION_FROM_EMAIL` 環境變數
- 支援的通知類型：
  - 歡迎通知（註冊成功）
  - API Key 異常通知
  - 通用告警通知
- 更新 `cmd/server/main.go`：fx 註冊 notification service

## Capabilities

### New Capabilities

- `email-notification`: Email 通知服務，使用 Resend API 發送，支援多種通知類型

### Modified Capabilities

（無既有 spec 需要修改）

## Impact

- **程式碼**：`notification/service.go`、`notification/resend.go`（新增）、`appconfig/config.go`、`main.go`
- **依賴**：`github.com/resend/resend-go/v2`（新增）
- **環境變數**：`RESEND_API_KEY`、`NOTIFICATION_FROM_EMAIL`（新增）
