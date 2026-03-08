package engine

import (
	"context"
	"time"

	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/bitfinex"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	minLendableBalance = 50.0
	staleTTL           = 15 * time.Minute
)

func ExecuteStrategy(ctx context.Context, bfx *bitfinex.Client, apiKey, apiSecret string, config domain.StrategyConfig, log *zap.Logger) {
	currency := config.Currency

	// 1. Get funding wallet balance
	wallet, err := bfx.GetFundingBalance(ctx, apiKey, apiSecret, currency)
	if err != nil {
		log.Warn("failed to get funding balance", zap.Error(err))
		return
	}

	// 2. Get active offers
	offers, err := bfx.GetActiveFundingOffers(ctx, apiKey, apiSecret, currency)
	if err != nil {
		log.Warn("failed to get active offers", zap.Error(err))
		return
	}

	// 3. Cancel stale offers (zombie TTL — spec §7.1)
	now := time.Now()
	for _, offer := range offers {
		if now.Sub(offer.CreatedAt) > staleTTL {
			log.Info("cancelling stale offer",
				zap.Int64("offerID", offer.ID),
				zap.Duration("age", now.Sub(offer.CreatedAt)),
			)
			if err := bfx.CancelFundingOffer(ctx, apiKey, apiSecret, offer.ID); err != nil {
				log.Warn("failed to cancel stale offer", zap.Int64("offerID", offer.ID), zap.Error(err))
			}
		}
	}

	// 4. Check if we should submit a new offer
	// Re-count active non-stale offers
	activeCount := 0
	for _, offer := range offers {
		if now.Sub(offer.CreatedAt) <= staleTTL {
			activeCount++
		}
	}

	if activeCount > 0 {
		log.Debug("active offer exists, skipping submission", zap.Int("activeCount", activeCount))
		return
	}

	// 5. Check minimum balance (spec §8.2)
	if wallet.BalanceAvailable < minLendableBalance {
		log.Debug("balance below minimum", zap.Float64("available", wallet.BalanceAvailable))
		return
	}

	// 6. Submit new offer
	amount := wallet.BalanceAvailable
	if amount > config.Amount.Max {
		amount = config.Amount.Max
	}

	params := domain.OfferParams{
		Currency: currency,
		Amount:   amount,
		Rate:     config.Rate.Min,
		Period:   config.Period.Min,
	}

	log.Info("submitting funding offer",
		zap.String("currency", currency),
		zap.Float64("amount", amount),
		zap.Float64("rate", params.Rate),
		zap.Int("period", params.Period),
	)

	offer, err := bfx.SubmitFundingOffer(ctx, apiKey, apiSecret, params)
	if err != nil {
		log.Warn("failed to submit offer", zap.Error(err))
		return
	}

	log.Info("offer submitted", zap.Int64("offerID", offer.ID))
}
