## 1. Domain

- [x] 1.1 Create `domain/execution.go`: define `ExecutionRecord` struct + action type constants

## 2. Database

- [x] 2.1 Add `executions` table to `schema/schema.hcl` (columns, indexes, FK)
- [x] 2.2 Run `atlas migrate diff` to generate migration
- [x] 2.3 Run `atlas migrate apply` to apply migration to Neon

## 3. Repository

- [x] 3.1 Create `repository/postgres/query/execution.sql`: sqlc queries (CreateExecution, ListByUser)
- [x] 3.2 Run `sqlc generate`
- [x] 3.3 Add `ExecutionRepository` interface to `repository/interfaces.go`
- [x] 3.4 Create `repository/postgres/execution.go`: implement ExecutionRepository
- [x] 3.5 Register `NewExecutionRepo` in `repository/postgres/di.go`

## 4. Service

- [x] 4.1 Create `service/execution.go`: `ExecutionService` with `ListByUser` method
- [x] 4.2 Create `service/execution_test.go`: test listing logic (mock repo)

## 5. Handler + Router

- [x] 5.1 Create `handler/execution.go`: `ExecutionHandler` with `List` method (GET /executions)
- [x] 5.2 Create `handler/execution_test.go`: test query param parsing and response
- [x] 5.3 Add routes to `handler/router.go`
- [x] 5.4 Add fx providers to `cmd/server/main.go`
