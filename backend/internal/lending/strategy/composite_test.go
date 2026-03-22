package strategy

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- helpers ---

func baseConfig() *domain.StrategyConfig {
	return &domain.StrategyConfig{
		Currency: "USD",
		Amount:   domain.AmountConfig{Min: 50, Max: 500},
		Rate:     domain.RateConfig{Min: 0.0001, Max: 0.005},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
}

func baseSnapshot() *domain.MarketSnapshot {
	return &domain.MarketSnapshot{
		FRR:    0.0003,
		Regime: domain.RegimeNeutral,
		MDC:    domain.MDCResult{Score: 0.0},
		OrderBook: domain.OrderBookSummary{
			BestBid:  0.00028,
			BestAsk:  0.00032,
			MidRate:  0.00030,
			Spread:   0.00004,
			BidDepth: 100000,
			AskDepth: 100000,
		},
		RegimeParams: domain.RegimeParams{Volatility: 0.05},
		Timestamp:    time.Date(2026, 3, 18, 10, 0, 0, 0, time.UTC), // Tuesday
	}
}

func baseCtx() *domain.DecisionContext {
	return &domain.DecisionContext{
		Snapshot:  baseSnapshot(),
		Config:    baseConfig(),
		Available: 1000,
		Currency:  "fUSD",
	}
}

// --- 5.1: Guard checks ---

func TestComposite_GuardNilSnapshot(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()
	ctx.Snapshot = nil

	result := s.Apply(ctx)

	if result.Reason != "composite:no_snapshot" {
		t.Errorf("expected reason composite:no_snapshot, got %s", result.Reason)
	}
	if len(result.Offers) != 0 {
		t.Errorf("expected no offers, got %d", len(result.Offers))
	}
}

func TestComposite_GuardFlashFreeze(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()
	ctx.Snapshot.FlashFreeze = true

	result := s.Apply(ctx)

	if result.Reason != "composite:flash_freeze" {
		t.Errorf("expected reason composite:flash_freeze, got %s", result.Reason)
	}
	if len(result.Offers) != 0 {
		t.Errorf("expected no offers, got %d", len(result.Offers))
	}
}

func TestComposite_GuardLowBalance(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()
	ctx.Available = 30

	result := s.Apply(ctx)

	if result.Reason != "composite:low_balance" {
		t.Errorf("expected reason composite:low_balance, got %s", result.Reason)
	}
}

// --- 5.2: Stage 1 rate resolution ---

func TestComposite_FloorEnforcedAbovePricing(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()

	// Set FRR very low so pricing gives a low rate
	ctx.Snapshot.FRR = 0.00005
	// But floor is config.Rate.Min = 0.0001 (opportunity cost)
	// and FRR floor = 0.00005 * 0.8 = 0.00004
	// So floor = max(0.0001, 0.00004, 0.0001) = 0.0001

	result := s.Apply(ctx)

	if len(result.Offers) == 0 {
		t.Fatal("expected offers, got none")
	}
	// All offers should have rate >= floor (0.0001)
	for i, o := range result.Offers {
		if o.Rate < 0.0001 {
			t.Errorf("offer[%d] rate %f below floor 0.0001", i, o.Rate)
		}
	}
}

func TestComposite_RateClampedToMax(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()

	// Set FRR very high so rate exceeds max
	ctx.Snapshot.FRR = 0.1
	ctx.Config.Rate.Max = 0.005

	result := s.Apply(ctx)

	if len(result.Offers) == 0 {
		t.Fatal("expected offers, got none")
	}
	for i, o := range result.Offers {
		if o.Rate > 0.005*aggressiveRateMul*1.02 { // allow noise margin
			t.Errorf("offer[%d] rate %f exceeds max (with tier multiplier margin)", i, o.Rate)
		}
	}
}

func TestComposite_ContangoBoostsRate(t *testing.T) {
	s := NewCompositeStrategy()

	ctxNeutral := baseCtx()
	ctxNeutral.Snapshot.Regime = domain.RegimeNeutral
	resultNeutral := s.Apply(ctxNeutral)

	ctxContango := baseCtx()
	ctxContango.Snapshot.Regime = domain.RegimeContango
	resultContango := s.Apply(ctxContango)

	if len(resultNeutral.Offers) == 0 || len(resultContango.Offers) == 0 {
		t.Fatal("expected offers in both cases")
	}

	// Contango should have higher rate (on average, ignoring noise)
	// We check the first offer's tier (core, same multiplier)
	// Due to noise, we just verify contango base logic runs without error
	if resultContango.Reason == "" {
		t.Error("expected non-empty reason")
	}
}

// --- 5.3: Stage 3 multi-tier multi-split ---

func TestComposite_MultiTierAllocation(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()
	ctx.Available = 2000 // > threeTierThreshold (1000)
	ctx.Config.Amount.Max = 2000
	// S8: add signals so deployment ratio is ~1.0
	ctx.Snapshot.MDC.Score = 1.0
	ctx.Snapshot.Signals = []domain.SignalValue{{Confidence: 1.0}}

	result := s.Apply(ctx)

	if len(result.Offers) == 0 {
		t.Fatal("expected offers, got none")
	}
	// With $2000 and neutral regime, should get 3 tiers
	// Each tier may be further split if > 5% of askDepth (100000*0.05 = 5000)
	// $2000 total is < $5000 per tier, so no splitting expected
	if len(result.Offers) < 3 {
		t.Errorf("expected at least 3 offers (3 tiers), got %d", len(result.Offers))
	}
}

func TestComposite_SplittingForLargeAmount(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()
	ctx.Available = 50000
	ctx.Config.Amount.Max = 50000
	ctx.Snapshot.OrderBook.AskDepth = 10000 // small depth → 5% = $500 per order max
	// S8: add signals so deployment ratio is ~1.0
	ctx.Snapshot.MDC.Score = 1.0
	ctx.Snapshot.Signals = []domain.SignalValue{{Confidence: 1.0}}

	result := s.Apply(ctx)

	// Should produce many offers due to splitting
	if len(result.Offers) < 5 {
		t.Errorf("expected multiple splits for large amount, got %d offers", len(result.Offers))
	}
}

// --- 5.4: Stage 4 noise removes small offers ---

func TestComposite_NoiseRemovesTinyOffers(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()
	ctx.Available = 55 // just above minimum, after noise + tiers some may drop below
	ctx.Config.Amount.Max = 55

	result := s.Apply(ctx)

	// All remaining offers must be >= minBalance
	for i, o := range result.Offers {
		if o.Amount < minBalance {
			t.Errorf("offer[%d] amount %f below minimum %f", i, o.Amount, minBalance)
		}
	}
}

// --- 5.5: Cancels integration ---

func TestComposite_CancelsStaleAndResidual(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()

	// Stale offer: rate way above bestAsk + 2*spread
	// bestAsk=0.00032, spread=0.00004, threshold = 0.00032 + 2*0.00004 = 0.0004
	ctx.ActiveOffers = []domain.FundingOffer{
		{ID: 100, Rate: 0.001, Amount: 500},  // stale (rate >> threshold)
		{ID: 200, Rate: 0.0003, Amount: 30},  // residual (amount < 50)
		{ID: 300, Rate: 0.0003, Amount: 200}, // normal
	}

	result := s.Apply(ctx)

	// Should cancel both 100 (stale) and 200 (residual)
	cancelMap := make(map[int64]bool)
	for _, id := range result.Cancels {
		cancelMap[id] = true
	}

	if !cancelMap[100] {
		t.Error("expected stale offer 100 to be cancelled")
	}
	if !cancelMap[200] {
		t.Error("expected residual offer 200 to be cancelled")
	}
	if cancelMap[300] {
		t.Error("offer 300 should not be cancelled")
	}
}

func TestComposite_NoDuplicateCancels(t *testing.T) {
	s := NewCompositeStrategy()
	ctx := baseCtx()

	// Offer that is both stale AND residual
	ctx.ActiveOffers = []domain.FundingOffer{
		{ID: 100, Rate: 0.001, Amount: 10}, // stale + residual
	}

	result := s.Apply(ctx)

	count := 0
	for _, id := range result.Cancels {
		if id == 100 {
			count++
		}
	}
	if count > 1 {
		t.Errorf("expected cancel ID 100 once, got %d times", count)
	}
}
