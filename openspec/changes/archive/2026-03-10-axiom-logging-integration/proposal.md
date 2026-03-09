## Why

後端目前用 zap 做結構化日誌，但只輸出到 stdout，沒有集中式日誌收集、查詢、或告警。改用 Axiom 全託管日誌平台取代原規劃的 Prometheus + Grafana。

## What Changes

- Go backend newLogger 增加 Axiom zap core（Tee 模式）
- appconfig.Config 新增 optional Axiom 欄位
- Frontend 新增 next-axiom + withAxiom wrapper
- 更新 ROADMAP.md 和 backend_architecture.md

## Capabilities

### New Capabilities
- `axiom-backend`: Go backend Axiom 整合
- `axiom-frontend`: Next.js Axiom 整合

### Modified Capabilities
(none)

## Impact

- Backend: cmd/server/main.go, internal/appconfig/config.go
- Frontend: next.config.ts, package.json, src/lib/axiom.ts
- 新依賴: axiom-go (Go), next-axiom (npm)
- 環境變數: AXIOM_TOKEN, AXIOM_DATASET, NEXT_PUBLIC_AXIOM_DATASET
