### Requirement: Load configuration from environment variables
The system SHALL load all configuration from environment variables at startup via `appconfig.Load()`.

#### Scenario: All required variables present
- **WHEN** all required environment variables are set
- **THEN** `appconfig.Load()` SHALL return a valid `Config` struct

#### Scenario: Missing required variable
- **WHEN** a required environment variable (e.g., `FRONTEND_URL`) is missing
- **THEN** `appconfig.Load()` SHALL return an error describing the missing variable, and the application SHALL fail to start

### Requirement: Configuration struct definition
The `Config` struct SHALL contain the following fields:

- `Port` (string, default: `"8080"`) — HTTP server port
- `FrontendURL` (string, required) — Vercel frontend URL for CORS
- `Environment` (string, default: `"development"`) — `development` or `production`
- `DatabaseURL` (string, required) — Neon PostgreSQL connection string

#### Scenario: Default values applied
- **WHEN** `PORT` is not set and `ENVIRONMENT` is not set
- **THEN** `Config.Port` SHALL be `"8080"` and `Config.Environment` SHALL be `"development"`

#### Scenario: Missing DatabaseURL
- **WHEN** `DATABASE_URL` environment variable is not set
- **THEN** `appconfig.Load()` SHALL return an error and the application SHALL fail to start

### Requirement: Configuration available via fx
The `Config` struct SHALL be provided to fx's dependency graph so that any component can receive it via constructor injection.

#### Scenario: Component receives config
- **WHEN** a component declares `appconfig.Config` as a constructor parameter
- **THEN** fx SHALL inject the loaded configuration
