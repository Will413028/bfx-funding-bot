package domain

import "time"

const (
	ActionPlace  = "place"
	ActionCancel = "cancel"
	ActionFilled = "filled"
	ActionRenew  = "renew"
)

type ExecutionRecord struct {
	ID           string
	UserID       string
	Action       string
	Currency     string
	Amount       float64
	Rate         float64
	Period       int
	OfferID      *int64
	Status       string
	ErrorMessage *string
	CreatedAt    time.Time
}
