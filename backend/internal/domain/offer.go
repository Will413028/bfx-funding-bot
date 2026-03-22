package domain

import "time"

type FundingOffer struct {
	CreatedAt time.Time
	UpdatedAt time.Time
	Currency  string
	Status    string
	ID        int64
	Amount    float64
	Rate      float64
	Period    int
}

type OfferParams struct {
	Currency string
	Amount   float64
	Rate     float64
	Period   int
}
