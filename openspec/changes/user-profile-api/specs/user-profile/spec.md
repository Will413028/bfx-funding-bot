## ADDED Requirements

### Requirement: Get current user profile

The system SHALL provide a `GET /api/v1/me` endpoint that returns the authenticated user's profile information.

#### Scenario: Successful profile retrieval

- **WHEN** an authenticated user sends `GET /api/v1/me`
- **THEN** the system SHALL respond with HTTP 200 and a JSON body containing `id`, `email`, `status`, `created_at`, and `updated_at` fields
- **THEN** the response SHALL NOT include `password_hash`

#### Scenario: Unauthenticated request

- **WHEN** a request is sent to `GET /api/v1/me` without a valid JWT token
- **THEN** the system SHALL respond with HTTP 401 Unauthorized

### Requirement: Change password

The system SHALL provide a `PUT /api/v1/me/password` endpoint that allows the authenticated user to change their password.

#### Scenario: Successful password change

- **WHEN** an authenticated user sends `PUT /api/v1/me/password` with a valid `current_password` and a `new_password` (at least 8 characters)
- **THEN** the system SHALL verify the current password, update the password hash, and respond with HTTP 200

#### Scenario: Wrong current password

- **WHEN** an authenticated user sends `PUT /api/v1/me/password` with an incorrect `current_password`
- **THEN** the system SHALL respond with HTTP 401 and error code `INVALID_CREDENTIALS`

#### Scenario: New password too short

- **WHEN** an authenticated user sends `PUT /api/v1/me/password` with a `new_password` shorter than 8 characters
- **THEN** the system SHALL respond with HTTP 400 and error code `VALIDATION_ERROR`
