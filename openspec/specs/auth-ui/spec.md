# auth-ui Specification

## Purpose
TBD - created by archiving change auth-pages. Update Purpose after archive.
## Requirements
### Requirement: Login form with validation
The LoginForm component SHALL provide email and password fields with client-side Zod validation, call the login Server Action on submit, and display server-side errors.

#### Scenario: Valid credentials
- **WHEN** user submits valid email and password
- **THEN** the form SHALL call the login Server Action
- **AND** on success, redirect to `callbackUrl` from URL params (default: `/overview`)

#### Scenario: Client validation failure
- **WHEN** user submits with empty email or invalid format
- **THEN** the form SHALL display translated validation error messages without calling the server

#### Scenario: Server error
- **WHEN** the login Server Action returns an error (e.g. invalid credentials)
- **THEN** the form SHALL display the error message above the submit button

### Requirement: Register form with validation
The RegisterForm component SHALL provide email and password fields with client-side Zod validation, call the register Server Action on submit (which auto-logs in on success).

#### Scenario: Successful registration
- **WHEN** user submits valid email and password
- **THEN** the form SHALL call the register Server Action
- **AND** on success (auto-login), redirect to `/overview`

#### Scenario: Duplicate email
- **WHEN** the register Server Action returns an error (e.g. email already exists)
- **THEN** the form SHALL display the server error message

### Requirement: Auth Zod schemas
The `lib/validations.ts` SHALL export `loginSchema` and `registerSchema` with email validation and password minimum length. Error messages SHALL use English keys for i18n translation.

#### Scenario: Login schema
- **WHEN** validating login input
- **THEN** email SHALL be required and valid format, password SHALL be required

#### Scenario: Register schema
- **WHEN** validating register input
- **THEN** email SHALL be required and valid format, password SHALL be minimum 8 characters

### Requirement: Premium dark theme for auth layout
The auth layout and form components SHALL follow the ui-premium-dark-theme specification: `bg-zinc-950` background, card with `bg-white/[0.02] border border-white/5`, glassmorphism on card container.

#### Scenario: Visual appearance
- **WHEN** the auth pages are rendered
- **THEN** the layout SHALL use `bg-zinc-950` as the full-screen background
- **AND** the form card SHALL have `bg-white/[0.02] border border-white/5 backdrop-blur-xl`

### Requirement: Navigation between login and register
Each auth page SHALL provide a link to the other page with translated text.

#### Scenario: Login page link to register
- **WHEN** user is on the login page
- **THEN** there SHALL be a link with text from `auth.noAccount` leading to the register page

#### Scenario: Register page link to login
- **WHEN** user is on the register page
- **THEN** there SHALL be a link with text from `auth.hasAccount` leading to the login page

### Requirement: Loading state during submission
The submit button SHALL show a loading state while the Server Action is in progress, preventing double submission.

#### Scenario: Form submitting
- **WHEN** user clicks submit and the action is in progress
- **THEN** the button SHALL be disabled and show a loading indicator

