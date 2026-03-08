## 1. Domain Layer

- [x] 1.1 Create `domain/config.go` — StrategyConfig struct (Currency, AmountConfig, RateConfig, PeriodConfig, AutoRenew) + UserConfig wrapper struct (ID, UserID, Config, CreatedAt, UpdatedAt)
- [x] 1.2 Implement `StrategyConfig.Validate()` — validate all fields per spec (currency required, amount min ≥ 50, rate positive, period 2-120, max ≥ min), collect all errors
- [x] 1.3 Add domain error factories — `ErrConfigNotFound()`, `ErrConfigValidation(details string)`

## 2. Database Layer

- [x] 2.1 Add `user_configs` table to `schema/schema.hcl` — id UUID PK, user_id UUID UNIQUE FK → users, config JSONB NOT NULL, created_at, updated_at
- [x] 2.2 Generate Atlas migration — `atlas migrate diff create_user_configs`
- [x] 2.3 Write `repository/postgres/query/config.sql` — sqlc queries: UpsertConfig (INSERT ON CONFLICT UPDATE), GetConfigByUserID, DeleteConfigByUserID
- [x] 2.4 Run `sqlc generate` and verify generated code

## 3. Repository Layer

- [x] 3.1 Add `ConfigRepository` interface to `repository/interfaces.go` — Upsert, GetByUserID, DeleteByUserID
- [x] 3.2 Create `repository/postgres/config.go` — implement ConfigRepository with sqlc, handle JSONB serialization/deserialization
- [x] 3.3 Register ConfigRepo in `repository/postgres/di.go` and `repository/di.go` fx modules

## 4. Service Layer

- [x] 4.1 Create `service/config.go` — ConfigService with Save (validate + upsert), Get (by userID), Delete (by userID)
- [x] 4.2 Write `service/config_test.go` — unit tests with mock repo: save success, save validation error, save duplicate (upsert), get exists, get not found, delete success, delete not found

## 5. Handler + Router

- [x] 5.1 Create `handler/config.go` — ConfigHandler with Save (PUT /configs), Get (GET /configs), Delete (DELETE /configs)
- [x] 5.2 Write `handler/config_test.go` — HTTP tests: PUT 200 success, PUT 400 validation, GET 200 found, GET 404, DELETE 204, DELETE 404
- [x] 5.3 Register config routes in `handler/router.go` under JWT-protected group
- [x] 5.4 Wire ConfigService + ConfigHandler in `cmd/server/main.go` via fx.Provide

## 6. Integration

- [x] 6.1 Run all tests (`go test ./...`) and verify pass
- [x] 6.2 Apply migration to Neon database
