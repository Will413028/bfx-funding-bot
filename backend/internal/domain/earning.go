package domain

import "time"

type FundingEarning struct {
	ID          int64
	Currency    string
	Amount      float64
	Balance     float64
	Description string
	Timestamp   time.Time
}
