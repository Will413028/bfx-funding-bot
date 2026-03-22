package domain

import "time"

type UserStatus string

const (
	UserStatusPending   UserStatus = "pending"
	UserStatusActive    UserStatus = "active"
	UserStatusSuspended UserStatus = "suspended"
)

type User struct {
	CreatedAt    time.Time
	UpdatedAt    time.Time
	ID           string
	Email        string
	PasswordHash string
	Status       UserStatus
	Plan         string
}
