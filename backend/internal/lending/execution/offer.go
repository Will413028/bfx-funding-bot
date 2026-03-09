package execution

import (
	"context"
	"fmt"

	"golang.org/x/time/rate"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// FundingClient abstracts Bitfinex funding API operations.
type FundingClient interface {
	SubmitFundingOffer(ctx context.Context, apiKey, apiSecret string, params domain.OfferParams) (*domain.FundingOffer, error)
	CancelFundingOffer(ctx context.Context, apiKey, apiSecret string, offerID int64) error
}

// KeyStore retrieves the user's API key and encrypted secret.
type KeyStore interface {
	GetByUserID(ctx context.Context, userID string) (*domain.APIKey, []byte, error)
}

// Cipher decrypts API key secrets.
type Cipher interface {
	Decrypt(ciphertext []byte) ([]byte, error)
}

// ExecutionLog persists execution records.
type ExecutionLog interface {
	Create(ctx context.Context, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error)
}

// ExecutionSummary reports the outcome of an ExecuteDecision call.
type ExecutionSummary struct {
	CancelOK   int
	CancelFail int
	PlaceOK    int
	PlaceFail  int
}

// OfferExecutor translates DecisionResult into Bitfinex API calls
// and records each operation.
type OfferExecutor struct {
	client  FundingClient
	keys    KeyStore
	cipher  Cipher
	log     ExecutionLog
	limiter *rate.Limiter
}

// NewOfferExecutor creates a new OfferExecutor.
func NewOfferExecutor(client FundingClient, keys KeyStore, cipher Cipher, log ExecutionLog, limiter *rate.Limiter) *OfferExecutor {
	return &OfferExecutor{
		client:  client,
		keys:    keys,
		cipher:  cipher,
		log:     log,
		limiter: limiter,
	}
}

// ExecuteDecision executes all cancels then all offers from a DecisionResult.
func (e *OfferExecutor) ExecuteDecision(ctx context.Context, userID string, decision *domain.DecisionResult) (*ExecutionSummary, error) {
	summary := &ExecutionSummary{}

	if decision == nil || (len(decision.Cancels) == 0 && len(decision.Offers) == 0) {
		return summary, nil
	}

	// Resolve API key
	apiKey, encSecret, err := e.keys.GetByUserID(ctx, userID)
	if err != nil {
		return nil, fmt.Errorf("get api key: %w", err)
	}
	if apiKey == nil {
		return nil, fmt.Errorf("no api key configured for user %s", userID)
	}

	secret, err := e.cipher.Decrypt(encSecret)
	if err != nil {
		return nil, fmt.Errorf("decrypt api secret: %w", err)
	}

	apiKeyStr := apiKey.APIKey
	apiSecretStr := string(secret)

	// Phase 1: Execute cancels
	for _, offerID := range decision.Cancels {
		if err := e.limiter.Wait(ctx); err != nil {
			summary.CancelFail++
			record := &domain.ExecutionRecord{
				UserID:   userID,
				Action:   domain.ActionCancel,
				Currency: decision.Currency,
				OfferID:  &offerID,
				Status:   "error",
			}
			errMsg := fmt.Sprintf("rate limiter: %v", err)
			record.ErrorMessage = &errMsg
			_, _ = e.log.Create(ctx, record)
			continue
		}

		cancelErr := e.client.CancelFundingOffer(ctx, apiKeyStr, apiSecretStr, offerID)

		record := &domain.ExecutionRecord{
			UserID:   userID,
			Action:   domain.ActionCancel,
			Currency: decision.Currency,
			OfferID:  &offerID,
		}

		if cancelErr != nil {
			summary.CancelFail++
			record.Status = "error"
			errMsg := cancelErr.Error()
			record.ErrorMessage = &errMsg
		} else {
			summary.CancelOK++
			record.Status = "success"
		}

		_, _ = e.log.Create(ctx, record) // best-effort audit log
	}

	// Phase 2: Execute offers
	for _, offer := range decision.Offers {
		if err := e.limiter.Wait(ctx); err != nil {
			summary.PlaceFail++
			record := &domain.ExecutionRecord{
				UserID:   userID,
				Action:   domain.ActionPlace,
				Currency: decision.Currency,
				Amount:   offer.Amount,
				Rate:     offer.Rate,
				Period:   offer.Period,
				Status:   "error",
			}
			errMsg := fmt.Sprintf("rate limiter: %v", err)
			record.ErrorMessage = &errMsg
			_, _ = e.log.Create(ctx, record)
			continue
		}

		params := domain.OfferParams{
			Currency: decision.Currency,
			Amount:   offer.Amount,
			Rate:     offer.Rate,
			Period:   offer.Period,
		}

		placed, placeErr := e.client.SubmitFundingOffer(ctx, apiKeyStr, apiSecretStr, params)

		record := &domain.ExecutionRecord{
			UserID:   userID,
			Action:   domain.ActionPlace,
			Currency: params.Currency,
			Amount:   offer.Amount,
			Rate:     offer.Rate,
			Period:   offer.Period,
		}

		if placeErr != nil {
			summary.PlaceFail++
			record.Status = "error"
			errMsg := placeErr.Error()
			record.ErrorMessage = &errMsg
		} else {
			summary.PlaceOK++
			record.Status = "success"
			if placed != nil {
				record.OfferID = &placed.ID
			}
		}

		_, _ = e.log.Create(ctx, record) // best-effort audit log
	}

	return summary, nil
}
