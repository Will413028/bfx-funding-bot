## ADDED Requirements

### Requirement: Config data hook
The `useConfig` hook SHALL fetch UserConfig via `apiClient.get<UserConfig>("/configs")` using TanStack Query with `configKeys.current()`. It SHALL treat 404 responses as "no config" (return null data) rather than an error.

#### Scenario: Config exists
- **WHEN** the user has a saved config
- **THEN** the hook SHALL return `{ data: UserConfig, isLoading, isError }`

#### Scenario: No config (404)
- **WHEN** the user has no saved config
- **THEN** the hook SHALL return `{ data: null, isLoading: false, isError: false }`

### Requirement: Save config mutation
The `useSaveConfig` mutation SHALL PUT to `/configs` with the full StrategyConfig payload and invalidate `configKeys.all` on success.

#### Scenario: Successful save
- **WHEN** the mutation is called with a valid StrategyConfig
- **THEN** it SHALL save the config and refresh the query cache

### Requirement: Reset config mutation
The `useResetConfig` mutation SHALL DELETE `/configs` and invalidate `configKeys.all` on success.

#### Scenario: Successful reset
- **WHEN** the mutation is called
- **THEN** it SHALL delete the config and refresh the query cache

### Requirement: Strategy config Zod schema
The `strategyConfigSchema` SHALL validate all StrategyConfig fields with cross-field refinements ensuring min ≤ max for amount, rate, and period ranges. Rate fields SHALL be in APR percentage format (user-facing), converted to daily rate for API submission.

#### Scenario: Valid config
- **WHEN** all fields are filled and min ≤ max for all ranges
- **THEN** validation SHALL pass

#### Scenario: Invalid range (min > max)
- **WHEN** amount.min > amount.max
- **THEN** validation SHALL fail with an appropriate error message

### Requirement: Strategy form component
The StrategyForm SHALL display three sections: basic settings (currency, autoRenew toggle), range parameters (amount/rate/period min-max number inputs), and action buttons (Save, Reset). It SHALL use react-hook-form with zodResolver. Rate inputs SHALL display as APR percentages.

#### Scenario: Editing existing config
- **WHEN** the user has a saved config
- **THEN** the form SHALL populate with the saved values (rate converted from daily to APR)

#### Scenario: New config (no saved config)
- **WHEN** the user has no saved config
- **THEN** the form SHALL populate with default values

#### Scenario: Save button
- **WHEN** the user clicks Save with valid inputs
- **THEN** the form SHALL submit the config (rate converted from APR to daily) and show a success indicator

#### Scenario: Reset button
- **WHEN** the user clicks Reset
- **THEN** the form SHALL show a confirmation prompt, and on confirm, delete the config and reset to defaults

### Requirement: Strategy page layout
The Strategy page SHALL display the StrategyForm with loading and error states. It SHALL use premium dark theme styling.

#### Scenario: Loading state
- **WHEN** the config is being fetched
- **THEN** the page SHALL show a loading spinner

#### Scenario: Form displayed
- **WHEN** data is loaded
- **THEN** the page SHALL render the StrategyForm with the config data
