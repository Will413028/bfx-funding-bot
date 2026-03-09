package service

import (
	"context"
	"sync"
	"time"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/crypto"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/lending/quota"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

type WalletSummary struct {
	Currency         string  `json:"currency"`
	Balance          float64 `json:"balance"`
	BalanceAvailable float64 `json:"balanceAvailable"`
}

type OfferSummary struct {
	ID        int64     `json:"id"`
	Currency  string    `json:"currency"`
	Amount    float64   `json:"amount"`
	Rate      float64   `json:"rate"`
	Period    int       `json:"period"`
	Status    string    `json:"status"`
	CreatedAt time.Time `json:"createdAt"`
}

type CreditSummary struct {
	ID        int64     `json:"id"`
	Currency  string    `json:"currency"`
	Amount    float64   `json:"amount"`
	Rate      float64   `json:"rate"`
	Period    int       `json:"period"`
	Status    string    `json:"status"`
	AutoRenew bool      `json:"autoRenew"`
	OpenedAt  time.Time `json:"openedAt"`
}

type MarketSummary struct {
	FRR         float64          `json:"frr"`
	Regime      domain.RegimeType `json:"regime"`
	MDCScore    float64          `json:"mdcScore"`
	FlashFreeze bool             `json:"flashFreeze"`
	Timestamp   time.Time        `json:"timestamp"`
}

type DashboardSummary struct {
	Wallet      *WalletSummary  `json:"wallet"`
	Offers      []OfferSummary  `json:"offers"`
	Credits     []CreditSummary `json:"credits"`
	Market      *MarketSummary  `json:"market"`
	EngineReady bool            `json:"engineReady"`
	Warnings    []string        `json:"warnings,omitempty"`
}

type DashboardService struct {
	bfx           *bitfinex.Client
	apiKeyRepo    repository.APIKeyRepository
	configRepo    repository.ConfigRepository
	snapshotCache repository.SnapshotCache
	cipher        *crypto.AES
	log           *zap.Logger
	limiterPool   *quota.RateLimiterPool
}

func NewDashboardService(bfx *bitfinex.Client, apiKeyRepo repository.APIKeyRepository, configRepo repository.ConfigRepository, snapshotCache repository.SnapshotCache, cipher *crypto.AES, log *zap.Logger, limiterPool *quota.RateLimiterPool) *DashboardService {
	return &DashboardService{
		bfx:           bfx,
		apiKeyRepo:    apiKeyRepo,
		configRepo:    configRepo,
		snapshotCache: snapshotCache,
		cipher:        cipher,
		log:           log,
		limiterPool:   limiterPool,
	}
}

func (s *DashboardService) GetSummary(ctx context.Context, userID string) (*DashboardSummary, error) {
	summary := &DashboardSummary{
		Offers:  []OfferSummary{},
		Credits: []CreditSummary{},
	}

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

	// Check config for engine_ready
	config, err := s.configRepo.GetByUserID(ctx, userID)
	if err == nil && config != nil {
		summary.EngineReady = true
	}

	// Parallel Bitfinex API calls
	currency := "USD"
	if config != nil {
		currency = config.Config.Currency
	}

	apiKey := key.APIKey
	apiSecret := string(secret)
	limiter := s.limiterPool.Get(userID)

	var (
		wallet   *domain.Wallet
		offers   []domain.FundingOffer
		credits  []domain.FundingCredit
		warnings []string
		mu       sync.Mutex
		wg       sync.WaitGroup
	)

	wg.Add(3)

	go func() {
		defer wg.Done()
		if err := limiter.Wait(ctx); err != nil {
			mu.Lock()
			warnings = append(warnings, "wallet_fetch_failed")
			mu.Unlock()
			return
		}
		w, err := s.bfx.GetFundingBalance(ctx, apiKey, apiSecret, currency)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			s.log.Warn("failed to fetch wallet", zap.Error(err))
			warnings = append(warnings, "wallet_fetch_failed")
		} else {
			wallet = w
		}
	}()

	go func() {
		defer wg.Done()
		if err := limiter.Wait(ctx); err != nil {
			mu.Lock()
			warnings = append(warnings, "offers_fetch_failed")
			mu.Unlock()
			return
		}
		o, err := s.bfx.GetActiveFundingOffers(ctx, apiKey, apiSecret, currency)
		mu.Lock()
		defer mu.Unlock()
		if err != nil {
			s.log.Warn("failed to fetch offers", zap.Error(err))
			warnings = append(warnings, "offers_fetch_failed")
		} else {
			offers = o
		}
	}()

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
			s.log.Warn("failed to fetch credits", zap.Error(err))
			warnings = append(warnings, "credits_fetch_failed")
		} else {
			credits = c
		}
	}()

	wg.Wait()
	summary.Warnings = warnings

	// Read market snapshot from Redis cache (non-fatal if missing)
	snapshot, err := s.snapshotCache.Get(ctx, "f"+currency)
	if err != nil {
		s.log.Warn("failed to read snapshot cache", zap.Error(err))
	}
	if snapshot != nil {
		summary.Market = &MarketSummary{
			FRR:         snapshot.FRR,
			Regime:      snapshot.Regime,
			MDCScore:    snapshot.MDC.Score,
			FlashFreeze: snapshot.FlashFreeze,
			Timestamp:   snapshot.Timestamp,
		}
	}

	// Map results
	if wallet != nil {
		summary.Wallet = &WalletSummary{
			Currency:         wallet.Currency,
			Balance:          wallet.Balance,
			BalanceAvailable: wallet.BalanceAvailable,
		}
	}

	for _, o := range offers {
		summary.Offers = append(summary.Offers, OfferSummary{
			ID:        o.ID,
			Currency:  o.Currency,
			Amount:    o.Amount,
			Rate:      o.Rate,
			Period:    o.Period,
			Status:    o.Status,
			CreatedAt: o.CreatedAt,
		})
	}

	for _, c := range credits {
		summary.Credits = append(summary.Credits, CreditSummary{
			ID:        c.ID,
			Currency:  c.Currency,
			Amount:    c.Amount,
			Rate:      c.Rate,
			Period:    c.Period,
			Status:    c.Status,
			AutoRenew: c.AutoRenew,
			OpenedAt:  c.OpenedAt,
		})
	}

	return summary, nil
}
