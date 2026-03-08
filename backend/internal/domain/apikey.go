package domain

import "time"

type APIKey struct {
	ID             string
	UserID         string
	Label          string
	APIKey         string
	ExchangeStatus string
	CreatedAt      time.Time
	UpdatedAt      time.Time
}
