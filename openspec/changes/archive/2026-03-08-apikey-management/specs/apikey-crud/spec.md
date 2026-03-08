## ADDED Requirements

### Requirement: Create API Key
The system SHALL allow an authenticated user to store a Bitfinex API Key. The request MUST include `api_key`, `api_secret`, and an optional `label`. The `api_secret` SHALL be encrypted with AES-256-GCM before storage. Each user SHALL have at most one API Key.

#### Scenario: Successful creation
- **WHEN** an authenticated user sends POST `/api/v1/apikeys` with valid `api_key`, `api_secret`, and `label`
- **THEN** the system stores the key with encrypted secret, returns HTTP 201 with `{ "id", "label", "api_key", "api_secret": "****", "created_at" }`

#### Scenario: Duplicate key for user
- **WHEN** an authenticated user who already has an API Key sends POST `/api/v1/apikeys`
- **THEN** the system returns HTTP 409 with error code `APIKEY_ALREADY_EXISTS`

#### Scenario: Missing required fields
- **WHEN** an authenticated user sends POST `/api/v1/apikeys` without `api_key` or `api_secret`
- **THEN** the system returns HTTP 400 with error code `VALIDATION_ERROR`

### Requirement: List API Keys
The system SHALL allow an authenticated user to retrieve their API Key. The response MUST NOT include the plaintext `api_secret`; it SHALL be masked as `"****"`.

#### Scenario: User has an API Key
- **WHEN** an authenticated user sends GET `/api/v1/apikeys`
- **THEN** the system returns HTTP 200 with an array containing the key with masked secret

#### Scenario: User has no API Key
- **WHEN** an authenticated user with no stored API Key sends GET `/api/v1/apikeys`
- **THEN** the system returns HTTP 200 with an empty array `[]`

### Requirement: Get API Key by ID
The system SHALL allow an authenticated user to retrieve a specific API Key by its ID. The response MUST mask `api_secret`. Users SHALL only access their own keys.

#### Scenario: Key exists and belongs to user
- **WHEN** an authenticated user sends GET `/api/v1/apikeys/:id` for their own key
- **THEN** the system returns HTTP 200 with the key details (secret masked)

#### Scenario: Key does not exist or belongs to another user
- **WHEN** an authenticated user sends GET `/api/v1/apikeys/:id` for a non-existent or others' key
- **THEN** the system returns HTTP 404 with error code `NOT_FOUND`

### Requirement: Delete API Key
The system SHALL allow an authenticated user to delete their API Key. Users SHALL only delete their own keys.

#### Scenario: Successful deletion
- **WHEN** an authenticated user sends DELETE `/api/v1/apikeys/:id` for their own key
- **THEN** the system deletes the key and returns HTTP 204

#### Scenario: Key does not exist or belongs to another user
- **WHEN** an authenticated user sends DELETE `/api/v1/apikeys/:id` for a non-existent or others' key
- **THEN** the system returns HTTP 404 with error code `NOT_FOUND`

### Requirement: API Key ownership enforcement
All API Key operations SHALL verify that the requested key belongs to the authenticated user. The system SHALL NOT allow cross-user access.

#### Scenario: Cross-user access attempt
- **WHEN** user A attempts to GET or DELETE an API Key belonging to user B
- **THEN** the system returns HTTP 404 (not 403, to prevent key enumeration)
