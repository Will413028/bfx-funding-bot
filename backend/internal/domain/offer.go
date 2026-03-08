package domain

import "time"

type FundingOffer struct {
	ID        int64
	Currency  string
	Amount    float64
	Rate      float64
	Period    int
	Status    string
	CreatedAt time.Time
	UpdatedAt time.Time
}

type OfferParams struct {
	Currency string
	Amount   float64
	Rate     float64
	Period   int
}
