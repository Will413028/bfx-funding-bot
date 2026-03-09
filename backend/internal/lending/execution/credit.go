package execution

import (
	"context"
	"fmt"
	"time"

	"golang.org/x/time/rate"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// RenewSummary reports the outcome of a ProcessCredits call.
type RenewSummary struct {
	RenewOK   int
	RenewFail int
	Skipped   int
}

// CreditManager handles auto-renewal of expiring funding credits.
type CreditManager struct {
	client  FundingClient
	keys    KeyStore
	cipher  Cipher
	log     ExecutionLog
	limiter *rate.Limiter
	now     func() time.Time
}

// NewCreditManager creates a new CreditManager.
func NewCreditManager(client FundingClient, keys KeyStore, cipher Cipher, log ExecutionLog, limiter *rate.Limiter) *CreditManager {
	return &CreditManager{
		client:  client,
		keys:    keys,
		cipher:  cipher,
		log:     log,
		limiter: limiter,
		now:     time.Now,
	}
}

// ProcessCredits scans credits and auto-renews those expiring within 24 hours
// that have AutoRenew=true.
func (cm *CreditManager) ProcessCredits(ctx context.Context, userID string, credits []domain.FundingCredit, config *domain.StrategyConfig) (*RenewSummary, error) {
	summary := &RenewSummary{}

	if len(credits) == 0 {
		return summary, nil
	}

	// Resolve API key
	apiKey, encSecret, err := cm.keys.GetByUserID(ctx, userID)
	if err != nil {
		return nil, fmt.Errorf("get api key: %w", err)
	}
	if apiKey == nil {
		return nil, fmt.Errorf("no api key configured for user %s", userID)
	}

	secret, err := cm.cipher.Decrypt(encSecret)
	if err != nil {
		return nil, fmt.Errorf("decrypt api secret: %w", err)
	}

	apiKeyStr := apiKey.APIKey
	apiSecretStr := string(secret)
	now := cm.now()

	for _, credit := range credits {
		// Calculate expiry: openedAt + period days
		expiresAt := credit.OpenedAt.AddDate(0, 0, credit.Period)
		remaining := expiresAt.Sub(now)

		// Skip if not expiring within 24 hours or not auto-renew
		if remaining > 24*time.Hour || !credit.AutoRenew {
			summary.Skipped++
			continue
		}

		// Determine renewal parameters
		rate := credit.Rate
		if config.Rate.Min > rate {
			rate = config.Rate.Min
		}
		period := credit.Period
		if config.Period.Min > period {
			period = config.Period.Min
		}

		if err := cm.limiter.Wait(ctx); err != nil {
			summary.RenewFail++
			record := &domain.ExecutionRecord{
				UserID:   userID,
				Action:   domain.ActionRenew,
				Currency: credit.Currency,
				Amount:   credit.Amount,
				Rate:     rate,
				Period:   period,
				Status:   "error",
			}
			errMsg := fmt.Sprintf("rate limiter: %v", err)
			record.ErrorMessage = &errMsg
			_, _ = cm.log.Create(ctx, record)
			continue
		}

		params := domain.OfferParams{
			Currency: credit.Currency,
			Amount:   credit.Amount,
			Rate:     rate,
			Period:   period,
		}

		placed, placeErr := cm.client.SubmitFundingOffer(ctx, apiKeyStr, apiSecretStr, params)

		record := &domain.ExecutionRecord{
			UserID:   userID,
			Action:   domain.ActionRenew,
			Currency: credit.Currency,
			Amount:   credit.Amount,
			Rate:     rate,
			Period:   period,
		}

		if placeErr != nil {
			summary.RenewFail++
			record.Status = "error"
			errMsg := placeErr.Error()
			record.ErrorMessage = &errMsg
		} else {
			summary.RenewOK++
			record.Status = "success"
			if placed != nil {
				record.OfferID = &placed.ID
			}
		}

		_, _ = cm.log.Create(ctx, record) // best-effort audit log
	}

	return summary, nil
}
