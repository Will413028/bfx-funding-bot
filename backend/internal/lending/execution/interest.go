package execution

import (
	"context"
	"fmt"

	"golang.org/x/time/rate"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// ReinvestSummary reports the outcome of a CheckAndReinvest call.
type ReinvestSummary struct {
	Placed bool
	Amount float64
	Reason string
}

// InterestCollector checks for idle available balance and reinvests it.
type InterestCollector struct {
	client  FundingClient
	keys    KeyStore
	cipher  Cipher
	log     ExecutionLog
	limiter *rate.Limiter
}

// NewInterestCollector creates a new InterestCollector.
func NewInterestCollector(client FundingClient, keys KeyStore, cipher Cipher, log ExecutionLog, limiter *rate.Limiter) *InterestCollector {
	return &InterestCollector{
		client:  client,
		keys:    keys,
		cipher:  cipher,
		log:     log,
		limiter: limiter,
	}
}

// CheckAndReinvest submits a funding offer if available balance meets the
// minimum threshold. Uses conservative parameters (Rate.Min, Period.Min).
func (ic *InterestCollector) CheckAndReinvest(ctx context.Context, userID string, available float64, config *domain.StrategyConfig) (*ReinvestSummary, error) {
	if available < config.Amount.Min {
		return &ReinvestSummary{
			Placed: false,
			Reason: "below_minimum",
		}, nil
	}

	// Resolve API key
	apiKey, encSecret, err := ic.keys.GetByUserID(ctx, userID)
	if err != nil {
		return nil, fmt.Errorf("get api key: %w", err)
	}
	if apiKey == nil {
		return nil, fmt.Errorf("no api key configured for user %s", userID)
	}

	secret, err := ic.cipher.Decrypt(encSecret)
	if err != nil {
		return nil, fmt.Errorf("decrypt api secret: %w", err)
	}

	apiKeyStr := apiKey.APIKey
	apiSecretStr := string(secret)

	// Cap amount at config max
	amount := available
	if amount > config.Amount.Max {
		amount = config.Amount.Max
	}

	params := domain.OfferParams{
		Currency: config.Currency,
		Amount:   amount,
		Rate:     config.Rate.Min,
		Period:   config.Period.Min,
	}

	if err := ic.limiter.Wait(ctx); err != nil {
		return nil, fmt.Errorf("rate limiter: %w", err)
	}

	placed, placeErr := ic.client.SubmitFundingOffer(ctx, apiKeyStr, apiSecretStr, params)

	record := &domain.ExecutionRecord{
		UserID:   userID,
		Action:   domain.ActionPlace,
		Currency: config.Currency,
		Amount:   amount,
		Rate:     config.Rate.Min,
		Period:   config.Period.Min,
	}

	if placeErr != nil {
		record.Status = "error"
		errMsg := placeErr.Error()
		record.ErrorMessage = &errMsg
		_, _ = ic.log.Create(ctx, record) // best-effort audit log
		return &ReinvestSummary{
			Placed: false,
			Amount: amount,
			Reason: "submit_failed",
		}, nil
	}

	record.Status = "success"
	if placed != nil {
		record.OfferID = &placed.ID
	}
	_, _ = ic.log.Create(ctx, record) // best-effort audit log

	return &ReinvestSummary{
		Placed: true,
		Amount: amount,
	}, nil
}
