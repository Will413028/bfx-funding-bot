package domain

import "time"

// Subscription plans
const (
	PlanFree       = "free"
	PlanStarter    = "starter"
	PlanPro        = "pro"
	PlanEnterprise = "enterprise"
)

// Billing statuses
const (
	BillingStatusPending = "pending"
	BillingStatusPaid    = "paid"
	BillingStatusOverdue = "overdue"
	BillingStatusWaived  = "waived"
)

// PlanPrices maps plan to monthly price in USD.
var PlanPrices = map[string]float64{
	PlanFree:       0,
	PlanStarter:    9.99,
	PlanPro:        29.99,
	PlanEnterprise: 99.99,
}

type PlanFeatures struct {
	Plan            string `json:"plan"`
	Price           float64 `json:"price"`
	AutoLending     bool   `json:"auto_lending"`
	AdvancedStrategy bool  `json:"advanced_strategy"`
	EmailNotify     bool   `json:"email_notify"`
	PriorityQuota   bool   `json:"priority_quota"`
	CustomParams    bool   `json:"custom_params"`
}

// GetPlanFeatures returns the feature set for a given plan.
func GetPlanFeatures(plan string) PlanFeatures {
	switch plan {
	case PlanEnterprise:
		return PlanFeatures{Plan: plan, Price: PlanPrices[plan], AutoLending: true, AdvancedStrategy: true, EmailNotify: true, PriorityQuota: true, CustomParams: true}
	case PlanPro:
		return PlanFeatures{Plan: plan, Price: PlanPrices[plan], AutoLending: true, AdvancedStrategy: true, EmailNotify: true, PriorityQuota: true}
	case PlanStarter:
		return PlanFeatures{Plan: plan, Price: PlanPrices[plan], AutoLending: true, EmailNotify: true}
	default:
		return PlanFeatures{Plan: PlanFree, Price: 0}
	}
}

type BillingRecord struct {
	ID          string
	UserID      string
	PeriodStart time.Time
	PeriodEnd   time.Time
	Plan        string
	Amount      float64
	Currency    string
	Status      string
	PaidAt      *time.Time
	CreatedAt   time.Time
}
