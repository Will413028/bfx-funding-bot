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
}

type DashboardService struct {
	bfx           *bitfinex.Client
	apiKeyRepo    repository.APIKeyRepository
	configRepo    repository.ConfigRepository
	snapshotCache repository.SnapshotCache
	cipher        *crypto.AES
}

func NewDashboardService(bfx *bitfinex.Client, apiKeyRepo repository.APIKeyRepository, configRepo repository.ConfigRepository, snapshotCache repository.SnapshotCache, cipher *crypto.AES) *DashboardService {
	return &DashboardService{
		bfx:           bfx,
		apiKeyRepo:    apiKeyRepo,
		configRepo:    configRepo,
		snapshotCache: snapshotCache,
		cipher:        cipher,
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

	var (
		wallet  *domain.Wallet
		offers  []domain.FundingOffer
		credits []domain.FundingCredit
		mu      sync.Mutex
		wg      sync.WaitGroup
	)

	wg.Add(3)

	go func() {
		defer wg.Done()
		w, err := s.bfx.GetFundingBalance(ctx, apiKey, apiSecret, currency)
		if err == nil {
			mu.Lock()
			wallet = w
			mu.Unlock()
		}
	}()

	go func() {
		defer wg.Done()
		o, err := s.bfx.GetActiveFundingOffers(ctx, apiKey, apiSecret, currency)
		if err == nil {
			mu.Lock()
			offers = o
			mu.Unlock()
		}
	}()

	go func() {
		defer wg.Done()
		c, err := s.bfx.GetActiveFundingCredits(ctx, apiKey, apiSecret, currency)
		if err == nil {
			mu.Lock()
			credits = c
			mu.Unlock()
		}
	}()

	wg.Wait()

	// Read market snapshot from Redis cache (non-fatal if missing)
	snapshot, _ := s.snapshotCache.Get(ctx, "f"+currency)
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
