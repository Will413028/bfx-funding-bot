package strategy

import (
	"math"
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// helper: build a minimal DecisionContext for testing
func testCtx(opts ...func(*domain.DecisionContext)) *domain.DecisionContext {
	ctx := &domain.DecisionContext{
		Snapshot: &domain.MarketSnapshot{
			FRR:    0.00025, // 0.025%/day
			Regime: domain.RegimeNeutral,
			MDC:    domain.MDCResult{Score: 0.0},
			OrderBook: domain.OrderBookSummary{
				BestBid:  0.00024,
				BestAsk:  0.00026,
				MidRate:  0.00025,
				Spread:   0.00002,
				BidDepth: 100000,
				AskDepth: 100000,
			},
		},
		Config: &domain.StrategyConfig{
			Currency: "fUSD",
			Amount:   domain.AmountConfig{Min: 50, Max: 10000},
			Rate:     domain.RateConfig{Min: 0.0001, Max: 0.005},
			Period:   domain.PeriodConfig{Min: 2, Max: 30},
		},
		Available: 5000,
		Currency:  "fUSD",
	}
	for _, fn := range opts {
		fn(ctx)
	}
	return ctx
}

func approxEqual(a, b, tol float64) bool {
	return math.Abs(a-b) < tol
}

// --- 3.1 MDC premium mapping ---

func TestPricing_MDC_Positive(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = 0.8
	})
	res := ps.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("expected 1 offer, got %d", len(res.Offers))
	}
	// base=0.00025, mdc_mul=1.0+0.8*0.5=1.4, regime=neutral(1.0)
	// rate = 0.00025 * 1.4 = 0.00035 → deviation 40% > neutral ceiling 25%
	// deviation guard clamps to FRR * 1.25 = 0.0003125
	expected := 0.00025 * 1.25
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("positive MDC: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_MDC_Negative(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = -0.6
	})
	res := ps.Apply(ctx)
	// base=0.00025, mdc_mul=1.0+(-0.6)*0.18=0.892
	expected := 0.00025 * 0.892
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("negative MDC: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_MDC_Zero(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx() // MDC=0, FRR=0.00025
	res := ps.Apply(ctx)
	// base=0.00025, mdc_mul=1.0, regime=neutral
	if !approxEqual(res.Offers[0].Rate, 0.00025, 1e-10) {
		t.Errorf("zero MDC: got %f, want 0.00025", res.Offers[0].Rate)
	}
}

// --- 3.2 Base rate: FRR vs fallback ---

func TestPricing_BaseRate_FRR(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0003
		c.Snapshot.OrderBook.MidRate = 0.00025
	})
	res := ps.Apply(ctx)
	// Should use FRR=0.0003, not MidRate
	if !approxEqual(res.Offers[0].Rate, 0.0003, 1e-10) {
		t.Errorf("FRR base rate: got %f, want 0.0003", res.Offers[0].Rate)
	}
}

func TestPricing_BaseRate_FallbackMidRate(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0 // zero FRR
		c.Snapshot.OrderBook.MidRate = 0.00025
	})
	res := ps.Apply(ctx)
	// Should fallback to MidRate=0.00025
	if !approxEqual(res.Offers[0].Rate, 0.00025, 1e-10) {
		t.Errorf("fallback base rate: got %f, want 0.00025", res.Offers[0].Rate)
	}
}

// --- 3.3 Regime adjustment ---

func TestPricing_Regime_Contango(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeContango
	})
	res := ps.Apply(ctx)
	// base=0.00025, mdc=1.0, regime=1.05
	expected := 0.00025 * 1.05
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("contango: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_Regime_Backwardation(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeBackwardation
	})
	res := ps.Apply(ctx)
	// base=0.00025, mdc=1.0, regime=0.90
	expected := 0.00025 * 0.90
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("backwardation: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_Regime_Crisis(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeCrisis
	})
	res := ps.Apply(ctx)
	// crisis → use config.Rate.Min directly
	if res.Offers[0].Rate != 0.0001 {
		t.Errorf("crisis: got %f, want 0.0001 (config.Rate.Min)", res.Offers[0].Rate)
	}
}

func TestPricing_Regime_Neutral(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx() // neutral by default
	res := ps.Apply(ctx)
	// base=0.00025, mdc=1.0, regime=1.0 (neutral)
	if !approxEqual(res.Offers[0].Rate, 0.00025, 1e-10) {
		t.Errorf("neutral: got %f, want 0.00025", res.Offers[0].Rate)
	}
}

// --- 3.4 Order book depth pressure ---

func TestPricing_DepthPressure_HighBid(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.OrderBook.BidDepth = 200000
		c.Snapshot.OrderBook.AskDepth = 100000 // ratio = 2.0 > 1.5
	})
	res := ps.Apply(ctx)
	// base=0.00025, mdc=1.0, regime=neutral, depth=1.02
	expected := 0.00025 * 1.02
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("high bid pressure: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_DepthPressure_Normal(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx() // equal depth, ratio=1.0
	res := ps.Apply(ctx)
	// No depth adjustment
	if !approxEqual(res.Offers[0].Rate, 0.00025, 1e-10) {
		t.Errorf("normal depth: got %f, want 0.00025", res.Offers[0].Rate)
	}
}

// --- 3.5 Wall avoidance ---

func TestPricing_WallAvoidance_NearbyWall(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.WallPositions = []domain.WallPosition{
			{Rate: 0.000252, Amount: 500000, Side: "offer"}, // within 10% of 0.00025
		}
	})
	res := ps.Apply(ctx)
	// G15: price just below the wall rate
	expected := 0.000252 - minTickSize
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("wall avoidance: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_WallAvoidance_NoWall(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx() // no walls
	res := ps.Apply(ctx)
	if !approxEqual(res.Offers[0].Rate, 0.00025, 1e-10) {
		t.Errorf("no wall: got %f, want 0.00025", res.Offers[0].Rate)
	}
}

func TestPricing_WallAvoidance_BidWallIgnored(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.WallPositions = []domain.WallPosition{
			{Rate: 0.000252, Amount: 500000, Side: "bid"}, // bid wall, should be ignored
		}
	})
	res := ps.Apply(ctx)
	if !approxEqual(res.Offers[0].Rate, 0.00025, 1e-10) {
		t.Errorf("bid wall should be ignored: got %f, want 0.00025", res.Offers[0].Rate)
	}
}

// --- 3.6 Rate clamping ---

func TestPricing_Clamp_BelowMin(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00005 // very low FRR
		c.Snapshot.MDC.Score = -1.0
		// rate = 0.00005 * 0.82 = 0.000041 < config.Rate.Min=0.0001
	})
	res := ps.Apply(ctx)
	if res.Offers[0].Rate != 0.0001 {
		t.Errorf("clamp below: got %f, want 0.0001", res.Offers[0].Rate)
	}
}

func TestPricing_Clamp_AboveMax(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.005 // very high FRR
		c.Snapshot.MDC.Score = 1.0
		// rate = 0.005 * 1.5 = 0.0075 > config.Rate.Max=0.005
	})
	res := ps.Apply(ctx)
	if res.Offers[0].Rate != 0.005 {
		t.Errorf("clamp above: got %f, want 0.005", res.Offers[0].Rate)
	}
}

func TestPricing_Clamp_WithinBounds(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx() // default rate 0.00025, within 0.0001-0.005
	res := ps.Apply(ctx)
	if res.Offers[0].Rate < 0.0001 || res.Offers[0].Rate > 0.005 {
		t.Errorf("rate %f should be within [0.0001, 0.005]", res.Offers[0].Rate)
	}
}

// --- 3.7 Flash freeze ---

func TestPricing_FlashFreeze(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})
	res := ps.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("flash freeze: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "flash_freeze" {
		t.Errorf("flash freeze: reason got %q, want %q", res.Reason, "flash_freeze")
	}
}

// --- 3.8 Insufficient balance ---

func TestPricing_InsufficientBalance(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 30 // below 50
	})
	res := ps.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("low balance: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "insufficient_balance" {
		t.Errorf("low balance: reason got %q, want %q", res.Reason, "insufficient_balance")
	}
}

// --- Additional: amount and period from config ---

func TestPricing_OfferAmountAndPeriod(t *testing.T) {
	ps := NewPricingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
	})
	res := ps.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("expected 1 offer, got %d", len(res.Offers))
	}
	// Amount = min(available=5000, config.Max=10000) = 5000
	if res.Offers[0].Amount != 5000 {
		t.Errorf("amount: got %f, want 5000", res.Offers[0].Amount)
	}
	// Period = config.Period.Min = 2
	if res.Offers[0].Period != 2 {
		t.Errorf("period: got %d, want 2", res.Offers[0].Period)
	}
}

// --- Adaptive Deviation Guard (§8.1) ---

func TestPricing_DeviationGuard_Neutral_Clamps(t *testing.T) {
	ps := NewPricingStrategy()
	// MDC=+1.0 → multiplier=1.5 → rate=0.000375, deviation=50% > neutral ceiling 25%
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = 1.0
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	res := ps.Apply(ctx)
	// Should be clamped to FRR * 1.25 = 0.0003125
	maxAllowed := 0.00025 * 1.25
	if res.Offers[0].Rate > maxAllowed+1e-10 {
		t.Errorf("neutral deviation guard: rate %f exceeds max allowed %f", res.Offers[0].Rate, maxAllowed)
	}
}

func TestPricing_DeviationGuard_Contango_WiderCeiling(t *testing.T) {
	ps := NewPricingStrategy()
	// MDC=+1.0 → multiplier=1.5, regime contango → *1.05 → deviation ~57.5% > 40%
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = 1.0
		c.Snapshot.Regime = domain.RegimeContango
	})
	res := ps.Apply(ctx)
	// Should be clamped to FRR * 1.40 = 0.00035
	maxAllowed := 0.00025 * 1.40
	if res.Offers[0].Rate > maxAllowed+1e-10 {
		t.Errorf("contango deviation guard: rate %f exceeds max allowed %f", res.Offers[0].Rate, maxAllowed)
	}
}

func TestPricing_DeviationGuard_Backwardation_TightCeiling(t *testing.T) {
	ps := NewPricingStrategy()
	// MDC=+0.5 → multiplier=1.25, regime backwardation → *0.90 → rate=0.00025*1.25*0.90=0.00028125
	// deviation = (0.00028125-0.00025)/0.00025 = 12.5% — within bear 20% ceiling
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = 0.5
		c.Snapshot.Regime = domain.RegimeBackwardation
	})
	res := ps.Apply(ctx)
	expected := 0.00025 * 1.25 * 0.90
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("backwardation within ceiling: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_DeviationGuard_Backwardation_Clamped(t *testing.T) {
	ps := NewPricingStrategy()
	// MDC=+1.0 → multiplier=1.5, regime backwardation → *0.90 → rate=0.00025*1.5*0.90=0.0003375
	// deviation = (0.0003375-0.00025)/0.00025 = 35% > bear 20% ceiling
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = 1.0
		c.Snapshot.Regime = domain.RegimeBackwardation
	})
	res := ps.Apply(ctx)
	maxAllowed := 0.00025 * 1.20
	if res.Offers[0].Rate > maxAllowed+1e-10 {
		t.Errorf("backwardation deviation guard: rate %f exceeds max allowed %f", res.Offers[0].Rate, maxAllowed)
	}
}

func TestPricing_DeviationGuard_NegativeDeviation(t *testing.T) {
	ps := NewPricingStrategy()
	// MDC=-1.0 → multiplier=0.7 → rate=0.000175, deviation=-30% — neutral ceiling 25%
	// Should clamp to FRR * 0.75 = 0.0001875
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = -1.0
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	res := ps.Apply(ctx)
	minAllowed := 0.00025 * 0.75
	if res.Offers[0].Rate < minAllowed-1e-10 {
		t.Errorf("negative deviation guard: rate %f below min allowed %f", res.Offers[0].Rate, minAllowed)
	}
}

func TestPricing_DeviationGuard_NoClampNeeded(t *testing.T) {
	ps := NewPricingStrategy()
	// MDC=+0.3 → multiplier=1.15, neutral → deviation=15% < 25% ceiling
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = 0.3
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	res := ps.Apply(ctx)
	expected := 0.00025 * 1.15
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("no clamp needed: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestPricing_CryptoPreset(t *testing.T) {
	cryptoPS := NewPricingStrategyWithPreset(domain.CryptoPreset)
	stablePS := NewPricingStrategy()

	// MDC=+1 → crypto maxPremiumUp=0.60, stablecoin=0.40
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.MDC.Score = 1.0
		c.Snapshot.Regime = domain.RegimeNeutral
	})

	cryptoRes := cryptoPS.Apply(ctx)
	stableRes := stablePS.Apply(ctx)

	if cryptoRes.Offers[0].Rate <= stableRes.Offers[0].Rate {
		t.Errorf("crypto should have higher rate at MDC=+1: crypto=%f, stablecoin=%f",
			cryptoRes.Offers[0].Rate, stableRes.Offers[0].Rate)
	}
}
