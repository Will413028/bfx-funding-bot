package domain

import "time"

type APIKey struct {
	CreatedAt      time.Time
	UpdatedAt      time.Time
	ID             string
	UserID         string
	Label          string
	APIKey         string
	ExchangeStatus string
}
