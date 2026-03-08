## ADDED Requirements

### Requirement: User data hook
The `useUser` hook SHALL fetch user profile via `apiClient.get<User>("/me")` using TanStack Query with key `["user", "me"]`.

#### Scenario: Successful fetch
- **WHEN** the hook is called in a mounted component
- **THEN** it SHALL return `{ data: User, isLoading, isError }` from TanStack Query

### Requirement: Change password mutation
The `useChangePassword` mutation SHALL PUT to `/me/password` with `{ currentPassword, newPassword }` payload.

#### Scenario: Successful password change
- **WHEN** the mutation is called with valid current and new passwords
- **THEN** it SHALL update the password and return success state

#### Scenario: Wrong current password
- **WHEN** the backend returns an error (wrong current password)
- **THEN** the mutation SHALL expose the error for display

### Requirement: Profile card display
The ProfileCard component SHALL display user email, account status badge, plan badge, and registration date. It SHALL use premium dark theme styling.

#### Scenario: Active user display
- **WHEN** the user status is "active"
- **THEN** the status badge SHALL show "Active" with `text-emerald-400`

#### Scenario: Suspended user display
- **WHEN** the user status is "suspended"
- **THEN** the status badge SHALL show "Suspended" with `text-rose-500`

### Requirement: Change password form
The ChangePasswordForm SHALL provide fields for currentPassword, newPassword, and confirmPassword. It SHALL validate with Zod schema ensuring newPassword matches confirmPassword and is at least 8 characters. It SHALL show success feedback after successful submission and reset the form.

#### Scenario: Valid submission
- **WHEN** user fills all fields correctly and passwords match
- **THEN** the form SHALL call changePassword mutation, show success state, and reset fields

#### Scenario: Password mismatch
- **WHEN** newPassword and confirmPassword do not match
- **THEN** the form SHALL show a validation error on confirmPassword

### Requirement: Settings page layout
The Settings page SHALL display ProfileCard and ChangePasswordForm in a single-column layout with loading state. It SHALL use premium dark theme styling.

#### Scenario: Loading state
- **WHEN** user data is being fetched
- **THEN** the page SHALL show a loading spinner

#### Scenario: Page displayed
- **WHEN** data is loaded
- **THEN** the page SHALL render ProfileCard and ChangePasswordForm
