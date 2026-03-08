package domain

import "time"

const (
	ActionPlace  = "place"
	ActionCancel = "cancel"
	ActionFilled = "filled"
	ActionRenew  = "renew"
)

type ExecutionRecord struct {
	ID           string    `json:"id"`
	UserID       string    `json:"userId"`
	Action       string    `json:"action"`
	Currency     string    `json:"currency"`
	Amount       float64   `json:"amount"`
	Rate         float64   `json:"rate"`
	Period       int       `json:"period"`
	OfferID      *int64    `json:"offerId,omitempty"`
	Status       string    `json:"status"`
	ErrorMessage *string   `json:"errorMessage,omitempty"`
	CreatedAt    time.Time `json:"createdAt"`
}
