## ADDED Requirements

### Requirement: API Key list hook
The `useApiKeys` hook SHALL fetch API Key list via `apiClient.get<ApiKey[]>("/api-keys")` using TanStack Query with `apiKeyKeys.list()`.

#### Scenario: Successful fetch
- **WHEN** the hook is called in a mounted component
- **THEN** it SHALL return `{ data: ApiKey[], isLoading, isError }` from TanStack Query

### Requirement: Create API Key mutation
The `useCreateApiKey` mutation SHALL POST to `/api-keys` with `{ label, apiKey, apiSecret }` and invalidate `apiKeyKeys.all` on success.

#### Scenario: Successful creation
- **WHEN** the mutation is called with valid label, apiKey, and apiSecret
- **THEN** it SHALL create the API Key and refresh the list

### Requirement: Delete API Key mutation
The `useDeleteApiKey` mutation SHALL DELETE `/api-keys/:id` and invalidate `apiKeyKeys.all` on success.

#### Scenario: Successful deletion
- **WHEN** the mutation is called with a valid API Key ID
- **THEN** it SHALL delete the API Key and refresh the list

### Requirement: Verify API Key mutation
The `useVerifyApiKey` mutation SHALL POST to `/api-keys/:id/verify` and invalidate `apiKeyKeys.all` on success.

#### Scenario: Successful verification
- **WHEN** the mutation is called with a valid API Key ID
- **THEN** it SHALL verify the key against Bitfinex and refresh the list with updated status

### Requirement: API Key card display
The ApiKeyCard component SHALL display label, masked API Key (first 8 chars + "..."), exchange status badge, and action buttons (Verify, Delete).

#### Scenario: Verified key display
- **WHEN** the API Key has exchangeStatus "verified"
- **THEN** the badge SHALL show "Verified" with `text-emerald-400` and display funding balance if available

#### Scenario: Unverified key display
- **WHEN** the API Key has exchangeStatus "unverified"
- **THEN** the badge SHALL show "Unverified" with `text-amber-500`

#### Scenario: Failed key display
- **WHEN** the API Key has exchangeStatus "failed"
- **THEN** the badge SHALL show "Failed" with `text-rose-500`

### Requirement: Create API Key dialog
The CreateApiKeyDialog SHALL provide a form with label, apiKey, and apiSecret fields, validated by Zod schema. It SHALL close and reset on successful submission.

#### Scenario: Valid submission
- **WHEN** user fills all fields and submits
- **THEN** the dialog SHALL call createApiKey mutation, close on success, and show loading state during submission

#### Scenario: Validation error
- **WHEN** user submits with empty fields
- **THEN** the form SHALL show validation error messages

### Requirement: Delete confirmation dialog
The DeleteConfirmDialog SHALL display a confirmation message with the API Key label and require explicit confirmation before deletion.

#### Scenario: User confirms deletion
- **WHEN** user clicks "Delete" in the confirmation dialog
- **THEN** the dialog SHALL call deleteApiKey mutation and close on success

#### Scenario: User cancels deletion
- **WHEN** user clicks "Cancel" in the confirmation dialog
- **THEN** the dialog SHALL close without deleting

### Requirement: API Keys page layout
The API Keys page SHALL display the list of API Key cards. It SHALL show a "Create" button when no API Key exists (single key limit). It SHALL use premium dark theme styling.

#### Scenario: No API Keys
- **WHEN** the user has no API Keys
- **THEN** the page SHALL display an empty state with a prompt to add their first API Key

#### Scenario: API Key exists
- **WHEN** the user has an API Key
- **THEN** the page SHALL display the API Key card and hide the "Create" button
