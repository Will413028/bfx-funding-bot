package domain

import (
	"fmt"
	"strings"
	"time"
)

const (
	minAmount     = 50.0
	maxDailyRate  = 0.01 // 1%/day = 365%/year
	minPeriodDays = 2
	maxPeriodDays = 120
)

// AllowedCurrencies is the whitelist of currencies accepted for funding.
var AllowedCurrencies = map[string]bool{
	"USD": true, "UST": true, "BTC": true, "ETH": true,
}

type AmountConfig struct {
	Min float64 `json:"min"`
	Max float64 `json:"max"`
}

type RateConfig struct {
	Min float64 `json:"min"`
	Max float64 `json:"max"`
}

type PeriodConfig struct {
	Min int `json:"min"`
	Max int `json:"max"`
}

type StrategyConfig struct {
	Currency  string       `json:"currency"`
	Amount    AmountConfig `json:"amount"`
	Rate      RateConfig   `json:"rate"`
	Period    PeriodConfig `json:"period"`
	AutoRenew bool         `json:"autoRenew"`
}

func (c StrategyConfig) Validate() error {
	var errs []string

	if !AllowedCurrencies[c.Currency] {
		errs = append(errs, "currency must be one of: USD, UST, BTC, ETH")
	}

	if c.Amount.Min < minAmount {
		errs = append(errs, fmt.Sprintf("minimum amount must be at least %.0f", minAmount))
	}
	if c.Amount.Max < c.Amount.Min {
		errs = append(errs, "max amount must be greater than or equal to min amount")
	}

	if c.Rate.Min <= 0 {
		errs = append(errs, "min rate must be positive")
	}
	if c.Rate.Max < c.Rate.Min {
		errs = append(errs, "max rate must be greater than or equal to min rate")
	}
	if c.Rate.Max > maxDailyRate {
		errs = append(errs, "max rate exceeds platform limit")
	}

	if c.Period.Min < minPeriodDays {
		errs = append(errs, fmt.Sprintf("min period must be at least %d days", minPeriodDays))
	}
	if c.Period.Max > maxPeriodDays {
		errs = append(errs, fmt.Sprintf("max period must not exceed %d days", maxPeriodDays))
	}
	if c.Period.Max < c.Period.Min {
		errs = append(errs, "max period must be greater than or equal to min period")
	}

	if len(errs) > 0 {
		return ErrValidation(strings.Join(errs, "; "))
	}
	return nil
}

type UserConfig struct {
	CreatedAt time.Time
	UpdatedAt time.Time
	ID        string
	UserID    string
	Config    StrategyConfig
}
