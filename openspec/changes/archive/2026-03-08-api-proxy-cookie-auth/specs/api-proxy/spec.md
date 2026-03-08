## ADDED Requirements

### Requirement: Catch-all proxy route
The proxy route at `app/api/proxy/[...path]/route.ts` SHALL forward requests to the Go backend by mapping `/api/proxy/<path>` to `API_URL/api/v1/<path>`.

#### Scenario: GET request forwarding
- **WHEN** a GET request is made to `/api/proxy/dashboard`
- **THEN** the proxy SHALL forward it to `API_URL/api/v1/dashboard` and return the backend response

#### Scenario: POST request forwarding
- **WHEN** a POST request is made to `/api/proxy/api-keys` with a JSON body
- **THEN** the proxy SHALL forward the method, body, and content-type to `API_URL/api/v1/api-keys`

### Requirement: Cookie to Bearer conversion
The proxy SHALL read the `auth_token` cookie and attach it as an `Authorization: Bearer <token>` header on the forwarded request.

#### Scenario: Authenticated request
- **WHEN** a request has an `auth_token` cookie with value `jwt123`
- **THEN** the forwarded request SHALL include header `Authorization: Bearer jwt123`

#### Scenario: Unauthenticated request
- **WHEN** a request has no `auth_token` cookie
- **THEN** the forwarded request SHALL NOT include an `Authorization` header (backend will return 401)

### Requirement: Query parameter preservation
The proxy SHALL preserve all query parameters from the original request.

#### Scenario: Request with query params
- **WHEN** a request is made to `/api/proxy/billing?limit=20&after=cursor123`
- **THEN** the forwarded request SHALL include `?limit=20&after=cursor123`

### Requirement: Response passthrough
The proxy SHALL return the backend's response status code and body to the client without modification.

#### Scenario: Success response
- **WHEN** the backend returns status 200 with JSON body
- **THEN** the proxy SHALL return status 200 with the same JSON body

#### Scenario: Error response
- **WHEN** the backend returns status 403 with error body
- **THEN** the proxy SHALL return status 403 with the same error body

### Requirement: Supported HTTP methods
The proxy SHALL export handlers for GET, POST, PUT, and DELETE methods.

#### Scenario: DELETE request
- **WHEN** a DELETE request is made to `/api/proxy/api-keys/abc123`
- **THEN** the proxy SHALL forward it as DELETE to `API_URL/api/v1/api-keys/abc123`
