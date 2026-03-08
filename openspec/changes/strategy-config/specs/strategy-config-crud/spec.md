## ADDED Requirements

### Requirement: StrategyConfig domain model
The system SHALL define a `domain.StrategyConfig` struct containing the following parameter groups:

- `Currency` (string, required) — 放貸幣種（e.g., "USD", "UST"）
- `Amount` — 放貸金額設定
  - `Min` (float64, required) — 最小單筆放貸金額（≥ 50 USD）
  - `Max` (float64, required) — 最大單筆放貸金額
- `Rate` — 日利率設定
  - `Min` (float64, required) — 最低可接受日利率（> 0）
  - `Max` (float64, required) — 最高掛單日利率
- `Period` — 放貸天數設定
  - `Min` (int, required) — 最短天數（≥ 2）
  - `Max` (int, required) — 最長天數（≤ 120）
- `AutoRenew` (bool, default: true) — 到期是否自動續借

#### Scenario: Valid StrategyConfig struct
- **WHEN** all fields are within valid ranges
- **THEN** `StrategyConfig.Validate()` SHALL return nil

### Requirement: user_configs database table
The system SHALL store strategy configs in a `user_configs` table with:
- `id` (UUID, primary key, auto-generated)
- `user_id` (UUID, NOT NULL, UNIQUE, foreign key → users.id ON DELETE CASCADE)
- `config` (JSONB, NOT NULL) — serialized StrategyConfig
- `created_at` (timestamptz, NOT NULL, default now())
- `updated_at` (timestamptz, NOT NULL, default now())

#### Scenario: One config per user
- **WHEN** a user already has a config and attempts to insert another
- **THEN** the UNIQUE constraint on `user_id` SHALL prevent duplicate entries

### Requirement: Save (upsert) strategy config
The system SHALL provide `PUT /configs` endpoint that creates or updates the user's strategy config.

#### Scenario: First time save
- **WHEN** an authenticated user sends `PUT /configs` with a valid config body and has no existing config
- **THEN** the system SHALL create a new config record and return 200 with the saved config (including `id`, `created_at`, `updated_at`)

#### Scenario: Update existing config
- **WHEN** an authenticated user sends `PUT /configs` with a valid config body and already has a config
- **THEN** the system SHALL update the existing config record and return 200 with the updated config

#### Scenario: Invalid config body
- **WHEN** the request body fails validation
- **THEN** the system SHALL return 400 with error details describing which fields are invalid

#### Scenario: Missing authentication
- **WHEN** the request has no valid JWT token
- **THEN** the system SHALL return 401

### Requirement: Get strategy config
The system SHALL provide `GET /configs` endpoint that retrieves the user's current strategy config.

#### Scenario: Config exists
- **WHEN** an authenticated user sends `GET /configs` and has a saved config
- **THEN** the system SHALL return 200 with the full config object

#### Scenario: No config exists
- **WHEN** an authenticated user sends `GET /configs` and has no saved config
- **THEN** the system SHALL return 404 with error code `NOT_FOUND`

### Requirement: Delete strategy config
The system SHALL provide `DELETE /configs` endpoint that removes the user's strategy config.

#### Scenario: Config exists
- **WHEN** an authenticated user sends `DELETE /configs` and has a saved config
- **THEN** the system SHALL delete the config record and return 204

#### Scenario: No config exists
- **WHEN** an authenticated user sends `DELETE /configs` and has no saved config
- **THEN** the system SHALL return 404 with error code `NOT_FOUND`

### Requirement: Config repository interface
The system SHALL define a `ConfigRepository` interface in `repository/interfaces.go`:

```
Upsert(ctx, userID string, configJSON []byte) (*domain.UserConfig, error)
GetByUserID(ctx, userID string) (*domain.UserConfig, error)
DeleteByUserID(ctx, userID string) error
```

#### Scenario: Repository methods used by service
- **WHEN** ConfigService calls repository methods
- **THEN** the repository SHALL handle PostgreSQL operations and return domain types

### Requirement: UserConfig wrapper type
The system SHALL define a `domain.UserConfig` struct wrapping the config with metadata:
- `ID` (string) — UUID
- `UserID` (string) — owner
- `Config` (StrategyConfig) — the actual strategy parameters
- `CreatedAt` (time.Time)
- `UpdatedAt` (time.Time)

#### Scenario: API response includes metadata
- **WHEN** config is returned from any endpoint
- **THEN** the response SHALL include `id`, `user_id`, `config`, `created_at`, `updated_at`
