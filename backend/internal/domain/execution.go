package domain

import "time"

const (
	ActionPlace  = "place"
	ActionCancel = "cancel"
	ActionFilled = "filled"
	ActionRenew  = "renew"
)

type ExecutionRecord struct {
	CreatedAt    time.Time `json:"createdAt"`
	OfferID      *int64    `json:"offerId,omitempty"`
	ErrorMessage *string   `json:"errorMessage,omitempty"`
	ID           string    `json:"id"`
	UserID       string    `json:"userId"`
	Action       string    `json:"action"`
	Currency     string    `json:"currency"`
	Status       string    `json:"status"`
	Amount       float64   `json:"amount"`
	Rate         float64   `json:"rate"`
	Period       int       `json:"period"`
}
