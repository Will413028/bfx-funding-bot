## MODIFIED Requirements

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
