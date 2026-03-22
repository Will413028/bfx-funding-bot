package domain

import "time"

type FundingEarning struct {
	Timestamp   time.Time
	Currency    string
	Description string
	ID          int64
	Amount      float64
	Balance     float64
}
