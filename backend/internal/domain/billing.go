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
	AutoLending      bool    `json:"autoLending"`
	AdvancedStrategy bool    `json:"advancedStrategy"`
	EmailNotify      bool    `json:"emailNotify"`
	PriorityQuota    bool    `json:"priorityQuota"`
	CustomParams     bool    `json:"customParams"`
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
	ID          string     `json:"id"`
	UserID      string     `json:"userId"`
	PeriodStart time.Time  `json:"periodStart"`
	PeriodEnd   time.Time  `json:"periodEnd"`
	Plan        string     `json:"plan"`
	Amount      float64    `json:"amount"`
	Currency    string     `json:"currency"`
	Status      string     `json:"status"`
	PaidAt      *time.Time `json:"paidAt,omitempty"`
	CreatedAt   time.Time  `json:"createdAt"`
}
