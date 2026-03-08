package domain

import "time"

type UserStatus string

const (
	UserStatusActive    UserStatus = "active"
	UserStatusSuspended UserStatus = "suspended"
)

type User struct {
	ID           string
	Email        string
	PasswordHash string
	Status       UserStatus
	Plan         string
	CreatedAt    time.Time
	UpdatedAt    time.Time
}
