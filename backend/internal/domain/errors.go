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
