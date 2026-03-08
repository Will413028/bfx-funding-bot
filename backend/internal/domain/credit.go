package domain

import "time"

type FundingCredit struct {
	ID        int64
	Currency  string
	Amount    float64
	Rate      float64
	Period    int
	Status    string
	AutoRenew bool
	OpenedAt  time.Time
}
