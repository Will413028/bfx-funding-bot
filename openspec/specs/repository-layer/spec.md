## ADDED Requirements

### Requirement: Repository interfaces in interfaces.go
All repository interfaces SHALL be defined in `repository/interfaces.go` using `domain/` types.

#### Scenario: UserRepository interface
- **WHEN** the repository layer is initialized
- **THEN** `repository.UserRepository` interface SHALL be defined with methods: `Create`, `GetByID`, `GetByEmail`

### Requirement: sqlc configuration
The `sqlc.yaml` SHALL be configured to generate type-safe Go code from SQL queries in `repository/postgres/query/` to `repository/postgres/sqlc/`.

#### Scenario: sqlc code generation
- **WHEN** a developer runs `make sqlc`
- **THEN** sqlc SHALL generate Go code in `repository/postgres/sqlc/` from SQL files in `repository/postgres/query/`

### Requirement: sqlc uses pgx/v5
The sqlc configuration SHALL use `sql_package: "pgx/v5"` to generate pgx-native code.

#### Scenario: Generated code uses pgx types
- **WHEN** sqlc generates code
- **THEN** the generated code SHALL import `pgx/v5` and use pgx types (not `database/sql`)

### Requirement: Postgres DAO wraps sqlc
Each postgres DAO file SHALL wrap the sqlc-generated `Queries` to implement the corresponding `repository.XxxRepository` interface, converting between sqlc models and `domain/` types.

#### Scenario: UserRepo implements UserRepository
- **WHEN** `postgres.UserRepo` is used
- **THEN** it SHALL implement `repository.UserRepository` and convert between `sqlc.User` and `domain.User`

### Requirement: fx module aggregation
The `repository/di.go` SHALL aggregate all sub-module fx providers. The `repository/postgres/di.go` SHALL register all postgres DAO providers.

#### Scenario: fx wires repository layer
- **WHEN** `repository.Module` is included in fx
- **THEN** all repository implementations SHALL be available for injection

### Requirement: Initial user queries
The initial SQL queries SHALL include basic user operations: create user, get user by ID, get user by email.

#### Scenario: user.sql contains required queries
- **WHEN** sqlc processes `repository/postgres/query/user.sql`
- **THEN** it SHALL generate Go functions for `CreateUser`, `GetUserByID`, `GetUserByEmail`
