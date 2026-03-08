package domain

import "fmt"

type AppError struct {
	StatusCode int    `json:"-"`
	Code       string `json:"code"`
	Message    string `json:"message"`
}

func (e *AppError) Error() string {
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}

func NewAppError(statusCode int, code, message string) *AppError {
	return &AppError{
		StatusCode: statusCode,
		Code:       code,
		Message:    message,
	}
}

func ErrNotFound(message string) *AppError {
	return NewAppError(404, "NOT_FOUND", message)
}

func ErrInternal(message string) *AppError {
	return NewAppError(500, "INTERNAL_ERROR", message)
}

func ErrValidation(message string) *AppError {
	return NewAppError(400, "VALIDATION_ERROR", message)
}

func ErrInvalidEmail() *AppError {
	return NewAppError(400, "INVALID_EMAIL", "Invalid email format")
}

func ErrPasswordTooShort() *AppError {
	return NewAppError(400, "PASSWORD_TOO_SHORT", "Password must be at least 8 characters")
}

func ErrEmailAlreadyExists() *AppError {
	return NewAppError(409, "EMAIL_ALREADY_EXISTS", "Email is already registered")
}

func ErrInvalidCredentials() *AppError {
	return NewAppError(401, "INVALID_CREDENTIALS", "Invalid email or password")
}

func ErrUserSuspended() *AppError {
	return NewAppError(403, "USER_SUSPENDED", "User account is suspended")
}

func ErrMissingToken() *AppError {
	return NewAppError(401, "MISSING_TOKEN", "Authorization token is required")
}

func ErrInvalidToken() *AppError {
	return NewAppError(401, "INVALID_TOKEN", "Invalid or malformed token")
}

func ErrTokenExpired() *AppError {
	return NewAppError(401, "TOKEN_EXPIRED", "Token has expired")
}

func ErrAPIKeyAlreadyExists() *AppError {
	return NewAppError(409, "APIKEY_ALREADY_EXISTS", "API key already exists for this user")
}

func ErrAPIKeyNotFound() *AppError {
	return NewAppError(404, "NOT_FOUND", "API key not found")
}
