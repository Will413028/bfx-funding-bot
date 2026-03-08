## MODIFIED Requirements

### Requirement: Configuration struct definition
The `Config` struct SHALL contain the following fields:

- `Port` (string, default: `"8080"`) — HTTP server port
- `FrontendURL` (string, required) — Vercel frontend URL for CORS
- `Environment` (string, default: `"development"`) — `development` or `production`
- `DatabaseURL` (string, required) — Neon PostgreSQL connection string
- `AESKey` ([]byte, required) — 256-bit key for AES-256-GCM encryption, loaded from hex-encoded `AES_KEY` env var

#### Scenario: Default values applied
- **WHEN** `PORT` is not set and `ENVIRONMENT` is not set
- **THEN** `Config.Port` SHALL be `"8080"` and `Config.Environment` SHALL be `"development"`

#### Scenario: Missing DatabaseURL
- **WHEN** `DATABASE_URL` environment variable is not set
- **THEN** `appconfig.Load()` SHALL return an error and the application SHALL fail to start

#### Scenario: Missing AES_KEY
- **WHEN** `AES_KEY` environment variable is not set
- **THEN** `appconfig.Load()` SHALL return an error and the application SHALL fail to start

#### Scenario: Invalid AES_KEY format
- **WHEN** `AES_KEY` is not valid hex or decodes to a length other than 32 bytes
- **THEN** `appconfig.Load()` SHALL return an error describing the invalid key format
