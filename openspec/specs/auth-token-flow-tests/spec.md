## ADDED Requirements

### Requirement: VerifyEmail service test coverage
UserService.VerifyEmail SHALL have unit tests covering success and error paths.

#### Scenario: Valid verification token activates user
- **WHEN** VerifyEmail is called with a token that exists in TokenRepository as type "verify"
- **THEN** the user's status SHALL be updated to "active"
- **AND** the verification token SHALL be deleted from TokenRepository

#### Scenario: Invalid or expired verification token
- **WHEN** VerifyEmail is called with a token that does not exist in TokenRepository
- **THEN** it SHALL return AppError with code "INVALID_OR_EXPIRED_TOKEN"

### Requirement: RequestPasswordReset service test coverage
UserService.RequestPasswordReset SHALL have unit tests covering success and non-existent email paths.

#### Scenario: Existing email triggers reset token storage
- **WHEN** RequestPasswordReset is called with an email that exists in UserRepository
- **THEN** it SHALL return nil (no error)
- **AND** a reset token SHALL be stored in TokenRepository

#### Scenario: Non-existent email returns nil
- **WHEN** RequestPasswordReset is called with an email that does not exist
- **THEN** it SHALL return nil (preventing email enumeration)

### Requirement: ResetPassword service test coverage
UserService.ResetPassword SHALL have unit tests covering success, invalid token, and password validation paths.

#### Scenario: Valid reset token updates password
- **WHEN** ResetPassword is called with a valid reset token and a valid new password
- **THEN** the user's password hash SHALL be updated
- **AND** the reset token SHALL be deleted from TokenRepository

#### Scenario: Invalid reset token
- **WHEN** ResetPassword is called with a token that does not exist in TokenRepository
- **THEN** it SHALL return AppError with code "INVALID_OR_EXPIRED_TOKEN"

#### Scenario: Password too short
- **WHEN** ResetPassword is called with a new password shorter than 8 characters
- **THEN** it SHALL return AppError with code "PASSWORD_TOO_SHORT"

#### Scenario: Password too long
- **WHEN** ResetPassword is called with a new password longer than 72 characters
- **THEN** it SHALL return AppError with code "PASSWORD_TOO_LONG"

### Requirement: RefreshToken service test coverage
UserService.RefreshToken SHALL have unit tests covering success and invalid token paths.

#### Scenario: Valid refresh token returns new token pair
- **WHEN** RefreshToken is called with a valid refresh token
- **THEN** it SHALL return a LoginResult with new accessToken, refreshToken, and expiresAt
- **AND** the old refresh token SHALL be deleted (rotation)
- **AND** a new refresh token SHALL be stored in TokenRepository

#### Scenario: Invalid refresh token
- **WHEN** RefreshToken is called with a token that does not exist in TokenRepository
- **THEN** it SHALL return AppError with code "INVALID_OR_EXPIRED_TOKEN"

### Requirement: Logout service test coverage
UserService.Logout SHALL have a unit test confirming it returns nil.

#### Scenario: Logout returns success
- **WHEN** Logout is called with any userID
- **THEN** it SHALL return nil

### Requirement: AuthHandler endpoint test coverage
All AuthHandler endpoints SHALL have HTTP-level tests using httptest and gin.TestMode.

#### Scenario: POST /auth/register success
- **WHEN** a valid register request is sent
- **THEN** it SHALL return HTTP 201 with user id and email

#### Scenario: POST /auth/register validation error
- **WHEN** a request with missing fields is sent
- **THEN** it SHALL return HTTP 400 with code "VALIDATION_ERROR"

#### Scenario: POST /auth/login success
- **WHEN** valid credentials are sent
- **THEN** it SHALL return HTTP 200 with accessToken, refreshToken, expiresAt

#### Scenario: POST /auth/login wrong password
- **WHEN** invalid credentials are sent
- **THEN** it SHALL return HTTP 401 with AppError

#### Scenario: POST /auth/refresh success
- **WHEN** a valid refreshToken is sent
- **THEN** it SHALL return HTTP 200 with new token pair

#### Scenario: POST /auth/refresh invalid token
- **WHEN** an invalid refreshToken is sent
- **THEN** it SHALL return HTTP 401 with code "INVALID_OR_EXPIRED_TOKEN"

#### Scenario: POST /auth/logout success
- **WHEN** logout is called
- **THEN** it SHALL return HTTP 200

#### Scenario: POST /auth/verify-email success
- **WHEN** a valid verification token is sent
- **THEN** it SHALL return HTTP 200

#### Scenario: POST /auth/verify-email invalid token
- **WHEN** an invalid token is sent
- **THEN** it SHALL return HTTP 401

#### Scenario: POST /auth/forgot-password always 200
- **WHEN** any email is sent to forgot-password
- **THEN** it SHALL return HTTP 200 regardless of whether the email exists

#### Scenario: POST /auth/reset-password success
- **WHEN** a valid reset token and new password are sent
- **THEN** it SHALL return HTTP 200

#### Scenario: POST /auth/reset-password invalid token
- **WHEN** an invalid token is sent
- **THEN** it SHALL return HTTP 401
