## ADDED Requirements

### Requirement: Unified error response format
All API error responses SHALL use the following JSON format:
```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable description"
  }
}
```

#### Scenario: Business logic error
- **WHEN** a handler encounters a business logic error (e.g., validation failure)
- **THEN** the response SHALL use the unified error format with the appropriate HTTP status code and error code

#### Scenario: Not found
- **WHEN** a request targets a non-existent resource
- **THEN** the response SHALL be status 404 with `{ "error": { "code": "NOT_FOUND", "message": "..." } }`

### Requirement: AppError type
The system SHALL define a `domain.AppError` struct containing `StatusCode` (int), `Code` (string), and `Message` (string), implementing the `error` interface.

#### Scenario: Creating an AppError
- **WHEN** a service returns `domain.NewAppError(400, "VALIDATION_ERROR", "invalid email")`
- **THEN** the handler SHALL respond with status 400 and `{ "error": { "code": "VALIDATION_ERROR", "message": "invalid email" } }`

### Requirement: Panic recovery
The system SHALL include Gin's Recovery middleware to catch panics and return a 500 error in the unified format.

#### Scenario: Handler panics
- **WHEN** a handler function panics during execution
- **THEN** the server SHALL recover, log the panic stack trace, and return status 500 with `{ "error": { "code": "INTERNAL_ERROR", "message": "Internal server error" } }`

### Requirement: Unknown route handling
The system SHALL return a 404 error for requests to undefined routes.

#### Scenario: Request to undefined route
- **WHEN** a request is made to `GET /api/v1/nonexistent`
- **THEN** the response SHALL be status 404 with `{ "error": { "code": "NOT_FOUND", "message": "Route not found" } }`
