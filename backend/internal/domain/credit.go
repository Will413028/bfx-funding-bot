package domain

import "time"

type FundingCredit struct {
	OpenedAt  time.Time
	Currency  string
	Status    string
	ID        int64
	Amount    float64
	Rate      float64
	Period    int
	AutoRenew bool
}
