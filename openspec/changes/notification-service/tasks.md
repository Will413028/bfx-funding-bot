## 1. Dependencies

- [x] 1.1 Add `github.com/resend/resend-go/v2`

## 2. Config

- [x] 2.1 Add `ResendAPIKey` and `NotificationFromEmail` to `appconfig/config.go` (both required)

## 3. Notification Package

- [x] 3.1 Create `notification/notifier.go`: define `Notifier` interface with `SendWelcome`, `SendAPIKeyAlert`, `SendAlert`
- [x] 3.2 Create `notification/resend.go`: implement `Notifier` using Resend SDK, construct from API key + from email
- [x] 3.3 Create `notification/resend_test.go`: test message construction (mock HTTP, verify request payload)

## 4. Wiring

- [x] 4.1 Update `cmd/server/main.go`: create Resend notifier and provide via fx
- [x] 4.2 Update `.env.example`: add `RESEND_API_KEY` and `NOTIFICATION_FROM_EMAIL`
