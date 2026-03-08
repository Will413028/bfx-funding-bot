# api-client Specification

## Purpose
TBD - created by archiving change core-lib-types. Update Purpose after archive.
## Requirements
### Requirement: Dual-environment base URL resolution
The api-client SHALL automatically resolve the base URL based on runtime environment: `/api/proxy` for Client Components, `API_URL + "/api/v1"` for Server Components / Server Actions.

#### Scenario: Client Component request
- **WHEN** a request is made from a Client Component (browser)
- **THEN** the request URL SHALL be prefixed with `/api/proxy`

#### Scenario: Server Component request
- **WHEN** a request is made from a Server Component or Server Action
- **THEN** the request URL SHALL be prefixed with `process.env.API_URL + "/api/v1"`

### Requirement: get() auto-unwraps data
The `get<T>(path, options?)` method SHALL unwrap the backend response `{ "data": T }` and return `T` directly.

#### Scenario: Single resource fetch
- **WHEN** calling `apiClient.get<UserConfig>("/configs")`
- **THEN** the returned value SHALL be of type `UserConfig` (unwrapped from `{ "data": UserConfig }`)

### Requirement: getList() preserves full response
The `getList<T>(path, options?)` method SHALL return the full response body without unwrapping, preserving pagination metadata.

#### Scenario: Paginated list fetch
- **WHEN** calling `apiClient.getList<BillingListResponse>("/billing", { params: { limit: "20" } })`
- **THEN** the returned value SHALL be of type `BillingListResponse` containing both `data` array and `pagination` object

### Requirement: Mutation methods (POST, PUT, DELETE)
The api-client SHALL provide `post<T>()`, `put<T>()`, and `del<T>()` methods that auto-unwrap `data` like `get()`.

#### Scenario: POST request
- **WHEN** calling `apiClient.post<ApiKey>("/api-keys", body)`
- **THEN** the request SHALL use POST method with JSON body and return unwrapped `T`

#### Scenario: DELETE request
- **WHEN** calling `apiClient.del("/api-keys/123")`
- **THEN** the request SHALL use DELETE method

### Requirement: Unified ApiError class
The api-client SHALL throw an `ApiError` (extends `Error`) for non-2xx responses, containing `status`, `code`, and `message` properties parsed from the backend error format `{ "error": { "code": "...", "message": "..." } }`.

#### Scenario: Backend returns 401
- **WHEN** the backend responds with status 401 and body `{ "error": { "code": "UNAUTHORIZED", "message": "Invalid token" } }`
- **THEN** the api-client SHALL throw an `ApiError` with `status: 401`, `code: "UNAUTHORIZED"`, `message: "Invalid token"`

#### Scenario: Non-JSON error response
- **WHEN** the backend responds with a non-2xx status and non-JSON body
- **THEN** the api-client SHALL throw an `ApiError` with the HTTP status and a generic error message

### Requirement: Query string support
The api-client SHALL accept an optional `params` object in request options and serialize it as URL query parameters.

#### Scenario: Request with query params
- **WHEN** calling `apiClient.getList<T>("/billing", { params: { limit: "20", after: "cursor123" } })`
- **THEN** the request URL SHALL include `?limit=20&after=cursor123`

### Requirement: Server-side auth header forwarding
When running on the server, the api-client SHALL support passing an `Authorization` header via request options for authenticated requests.

#### Scenario: Server Action with auth
- **WHEN** a Server Action calls `apiClient.get<User>("/users/me", { headers: { Authorization: "Bearer xxx" } })`
- **THEN** the request SHALL include the `Authorization: Bearer xxx` header

