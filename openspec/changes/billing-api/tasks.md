## 1. Domain

- [x] 1.1 Create `domain/billing.go`: define `BillingRecord` struct, plan constants, `PlanFeatures`, `GetPlanFeatures()`

## 2. Database

- [x] 2.1 Add `billing_records` table + `users.plan` column to `schema/schema.hcl`
- [x] 2.2 Run `atlas migrate diff` to generate migration
- [x] 2.3 Run `atlas migrate apply` to apply migration to Neon

## 3. Repository

- [x] 3.1 Create `repository/postgres/query/billing.sql`: sqlc queries (CreateBillingRecord, ListByUser)
- [x] 3.2 Run `sqlc generate` (also regenerated user queries for new plan column)
- [x] 3.3 Add `BillingRepository` interface to `repository/interfaces.go`
- [x] 3.4 Create `repository/postgres/billing.go`: implement BillingRepository
- [x] 3.5 Register in `repository/postgres/di.go` and `repository/di.go`

## 4. Service

- [x] 4.1 Create `service/billing.go`: `BillingService` with `GetBilling` + `GetPlan` methods
- [x] 4.2 Create `service/billing_test.go`: test summary calculation + plan features

## 5. Handler + Router

- [x] 5.1 Create `handler/billing.go`: `BillingHandler` with `Get` + `GetPlan` methods
- [x] 5.2 Create `handler/billing_test.go`: test endpoints
- [x] 5.3 Add routes to `handler/router.go` (GET /billing, GET /billing/plan)
- [x] 5.4 Add fx providers to `cmd/server/main.go`
