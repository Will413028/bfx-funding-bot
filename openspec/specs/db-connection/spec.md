## ADDED Requirements

### Requirement: PostgreSQL connection pool via pgxpool
The system SHALL establish a PostgreSQL connection pool using `pgxpool.New()` with the `DATABASE_URL` from configuration.

#### Scenario: Successful connection
- **WHEN** the application starts with a valid `DATABASE_URL`
- **THEN** a `*pgxpool.Pool` SHALL be created and available for injection via fx

#### Scenario: Invalid connection string
- **WHEN** `DATABASE_URL` is malformed or the database is unreachable
- **THEN** the application SHALL fail to start with a descriptive error

### Requirement: Connection pool lifecycle managed by fx
The `*pgxpool.Pool` SHALL be registered with fx lifecycle, closing the pool on application shutdown.

#### Scenario: Application shutdown
- **WHEN** fx triggers shutdown (SIGTERM)
- **THEN** `pool.Close()` SHALL be called, releasing all connections

### Requirement: infra package provides only connection
The `infra/` package SHALL only create and expose `*pgxpool.Pool`. It SHALL NOT contain any query logic.

#### Scenario: infra module registration
- **WHEN** `infra.Module` is included in fx
- **THEN** `*pgxpool.Pool` SHALL be available for constructor injection by any component

### Requirement: Neon TLS requirement
The connection SHALL use `sslmode=require` as required by Neon. The system SHALL NOT attempt to connect without TLS.

#### Scenario: Connection to Neon
- **WHEN** connecting to a Neon PostgreSQL instance
- **THEN** the connection SHALL use TLS (`sslmode=require` in the connection string)
