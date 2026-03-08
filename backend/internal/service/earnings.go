package service

import (
	"context"
	"sync"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type EarningsSummary struct {
	EstimatedDailyEarning float64 `json:"estimatedDailyEarning"`
	WeightedAPY           float64 `json:"weightedAPY"`
	Earnings7d            float64 `json:"earnings7d"`
	Earnings30d           float64 `json:"earnings30d"`
	TotalLent             float64 `json:"totalLent"`
	ActiveCredits         int     `json:"activeCredits"`
	Currency              string  `json:"currency"`
}

type EarningsService struct {
	bfx        *bitfinex.Client
	apiKeyRepo repository.APIKeyRepository
	configRepo repository.ConfigRepository
	cipher     *crypto.AES
}

func NewEarningsService(bfx *bitfinex.Client, apiKeyRepo repository.APIKeyRepository, configRepo repository.ConfigRepository, cipher *crypto.AES) *EarningsService {
	return &EarningsService{
		bfx:        bfx,
		apiKeyRepo: apiKeyRepo,
		configRepo: configRepo,
		cipher:     cipher,
	}
}

func (s *EarningsService) GetEarnings(ctx context.Context, userID string) (*EarningsSummary, error) {
	summary := &EarningsSummary{}

	// Check API key
	key, encryptedSecret, err := s.apiKeyRepo.GetByUserID(ctx, userID)
	if err != nil {
		return nil, domain.ErrInternal("failed to query API key")
	}
	if key == nil || key.ExchangeStatus != "verified" {
		return summary, nil
	}

	// Decrypt secret
	secret, err := s.cipher.Decrypt(encryptedSecret)
	if err != nil {
		return nil, domain.ErrInternal("failed to decrypt API secret")
	}

	// Determine currency
	currency := "USD"
	config, err := s.configRepo.GetByUserID(ctx, userID)
	if err == nil && config != nil {
		currency = config.Config.Currency
	}
	summary.Currency = currency

	apiKey := key.APIKey
	apiSecret := string(secret)
	now := time.Now()

	var (
		credits    []domain.FundingCredit
		earnings7  []domain.FundingEarning
		earnings30 []domain.FundingEarning
		mu         sync.Mutex
		wg         sync.WaitGroup
	)

	wg.Add(3)

	go func() {
		defer wg.Done()
		c, err := s.bfx.GetActiveFundingCredits(ctx, apiKey, apiSecret, currency)
		if err == nil {
			mu.Lock()
			credits = c
			mu.Unlock()
		}
	}()

	go func() {
		defer wg.Done()
		e, err := s.bfx.GetFundingEarnings(ctx, apiKey, apiSecret, currency, now.AddDate(0, 0, -7), now)
		if err == nil {
			mu.Lock()
			earnings7 = e
			mu.Unlock()
		}
	}()

	go func() {
		defer wg.Done()
		e, err := s.bfx.GetFundingEarnings(ctx, apiKey, apiSecret, currency, now.AddDate(0, 0, -30), now)
		if err == nil {
			mu.Lock()
			earnings30 = e
			mu.Unlock()
		}
	}()

	wg.Wait()

	// Calculate from active credits
	var totalWeightedRate, totalLent float64
	for _, c := range credits {
		totalLent += c.Amount
		totalWeightedRate += c.Amount * c.Rate
	}

	summary.ActiveCredits = len(credits)
	summary.TotalLent = totalLent
	summary.EstimatedDailyEarning = totalWeightedRate
	if totalLent > 0 {
		summary.WeightedAPY = (totalWeightedRate / totalLent) * 365
	}

	// Sum historical earnings
	for _, e := range earnings7 {
		summary.Earnings7d += e.Amount
	}
	for _, e := range earnings30 {
		summary.Earnings30d += e.Amount
	}

	return summary, nil
}
