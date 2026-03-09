package service

import (
	"context"
	"sort"
	"sync"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/quota"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type DailyEarning struct {
	Date   string  `json:"date"`
	Amount float64 `json:"amount"`
}

type EarningsSummary struct {
	EstimatedDailyEarning float64  `json:"estimatedDailyEarning"`
	WeightedAPY           float64  `json:"weightedAPY"`
	Earnings7d            float64  `json:"earnings7d"`
	Earnings30d           float64  `json:"earnings30d"`
	TotalLent             float64  `json:"totalLent"`
	ActiveCredits         int      `json:"activeCredits"`
	Currency              string   `json:"currency"`
	Warnings              []string `json:"warnings,omitempty"`
}

type EarningsService struct {
	bfx         *bitfinex.Client
	apiKeyRepo  repository.APIKeyRepository
	configRepo  repository.ConfigRepository
	cipher      *crypto.AES
	limiterPool *quota.RateLimiterPool
}

func NewEarningsService(bfx *bitfinex.Client, apiKeyRepo repository.APIKeyRepository, configRepo repository.ConfigRepository, cipher *crypto.AES, limiterPool *quota.RateLimiterPool) *EarningsService {
	return &EarningsService{
		bfx:         bfx,
		apiKeyRepo:  apiKeyRepo,
		configRepo:  configRepo,
		cipher:      cipher,
		limiterPool: limiterPool,
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
	limiter := s.limiterPool.Get(userID)

	var (
		credits    []domain.FundingCredit
		earnings7  []domain.FundingEarning
		earnings30 []domain.FundingEarning
		warnings   []string
		mu         sync.Mutex
		wg         sync.WaitGroup
	)

	wg.Add(3)

	go func() {
		defer wg.Done()
		if err := limiter.Wait(ctx); err != nil {
			mu.Lock()
			warnings = append(warnings, "credits_fetch_failed")
			mu.Unlock()
			return
		}
		c, err := s.bfx.GetActiveFundingCredits(ctx, apiKey, apiSecret, currency)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			warnings = append(warnings, "credits_fetch_failed")
		} else {
			credits = c
		}
	}()

	go func() {
		defer wg.Done()
		if err := limiter.Wait(ctx); err != nil {
			mu.Lock()
			warnings = append(warnings, "earnings_7d_fetch_failed")
			mu.Unlock()
			return
		}
		e, err := s.bfx.GetFundingEarnings(ctx, apiKey, apiSecret, currency, now.AddDate(0, 0, -7), now)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			warnings = append(warnings, "earnings_7d_fetch_failed")
		} else {
			earnings7 = e
		}
	}()

	go func() {
		defer wg.Done()
		if err := limiter.Wait(ctx); err != nil {
			mu.Lock()
			warnings = append(warnings, "earnings_30d_fetch_failed")
			mu.Unlock()
			return
		}
		e, err := s.bfx.GetFundingEarnings(ctx, apiKey, apiSecret, currency, now.AddDate(0, 0, -30), now)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			warnings = append(warnings, "earnings_30d_fetch_failed")
		} else {
			earnings30 = e
		}
	}()

	wg.Wait()
	summary.Warnings = warnings

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

func (s *EarningsService) GetEarningsHistory(ctx context.Context, userID string, days int) ([]DailyEarning, error) {
	if days <= 0 {
		days = 30
	}
	if days > 90 {
		days = 90
	}

	key, encryptedSecret, err := s.apiKeyRepo.GetByUserID(ctx, userID)
	if err != nil {
		return nil, domain.ErrInternal("failed to query API key")
	}
	if key == nil || key.ExchangeStatus != "verified" {
		return []DailyEarning{}, nil
	}

	secret, err := s.cipher.Decrypt(encryptedSecret)
	if err != nil {
		return nil, domain.ErrInternal("failed to decrypt API secret")
	}

	currency := "USD"
	config, err := s.configRepo.GetByUserID(ctx, userID)
	if err == nil && config != nil {
		currency = config.Config.Currency
	}

	now := time.Now().UTC()
	start := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC).AddDate(0, 0, -days+1)
	end := now

	if err := s.limiterPool.Get(userID).Wait(ctx); err != nil {
		return nil, domain.ErrInternal("rate limit wait cancelled")
	}

	earnings, err := s.bfx.GetFundingEarnings(ctx, key.APIKey, string(secret), currency, start, end)
	if err != nil {
		return nil, domain.ErrInternal("failed to fetch earnings from exchange")
	}

	// Bucket by UTC date
	buckets := make(map[string]float64)
	for _, e := range earnings {
		dateKey := e.Timestamp.UTC().Format("2006-01-02")
		buckets[dateKey] += e.Amount
	}

	// Fill gaps and build result
	result := make([]DailyEarning, 0, days)
	for d := start; !d.After(now); d = d.AddDate(0, 0, 1) {
		dateKey := d.Format("2006-01-02")
		result = append(result, DailyEarning{
			Date:   dateKey,
			Amount: buckets[dateKey],
		})
	}

	sort.Slice(result, func(i, j int) bool {
		return result[i].Date < result[j].Date
	})

	return result, nil
}
