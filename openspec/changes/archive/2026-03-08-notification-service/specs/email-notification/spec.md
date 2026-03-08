## ADDED Requirements

### Requirement: Notifier interface

The system SHALL define a `Notifier` interface in the `notification` package with methods for sending different notification types.

#### Scenario: Interface available for injection

- **WHEN** a service needs to send notifications
- **THEN** it SHALL depend on the `Notifier` interface, which is provided by fx dependency injection

### Requirement: Send welcome email

The system SHALL send a welcome email to the user upon successful registration.

#### Scenario: Welcome email sent

- **WHEN** `SendWelcome` is called with a valid email address
- **THEN** the system SHALL send an email with subject "Welcome to BFX Funding Bot" to that address via Resend API

#### Scenario: Email send failure

- **WHEN** the Resend API returns an error
- **THEN** the method SHALL return the error (caller decides how to handle)

### Requirement: Send API key alert

The system SHALL support sending API key related alert emails (key expired, permissions invalid, etc).

#### Scenario: API key alert sent

- **WHEN** `SendAPIKeyAlert` is called with an email and alert message
- **THEN** the system SHALL send an email with subject "API Key Alert" containing the alert message

### Requirement: Send general alert

The system SHALL support sending generic alert emails with custom subject and message.

#### Scenario: General alert sent

- **WHEN** `SendAlert` is called with email, subject, and message
- **THEN** the system SHALL send an email with the specified subject and message body

### Requirement: Configuration via environment variables

The system SHALL read `RESEND_API_KEY` and `NOTIFICATION_FROM_EMAIL` from environment variables.

#### Scenario: Missing API key

- **WHEN** the application starts without `RESEND_API_KEY` set
- **THEN** the system SHALL fail to start with a descriptive error
