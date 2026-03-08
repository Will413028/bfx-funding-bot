package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- 2.1 Basic lockup cost ---

func TestLockup_BasicCost(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.001 // high enough that cost won't exceed threshold
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.RegimeParams.Volatility = 0.10
		c.Config.Period.Min = 30
	})
	res := ls.Apply(ctx)
	// cost = 0.000005 × 30 × (1 + 5.0 × 0.10) = 0.000005 × 30 × 1.5 = 0.000225
	// rate = 0.001 + 0.000225 = 0.001225
	expected := 0.001 + 0.000225
	if !approxEqual(res.Offers[0].Rate, expected, 1e-9) {
		t.Errorf("basic cost: got %f, want %f", res.Offers[0].Rate, expected)
	}
	if res.Offers[0].Period != 30 {
		t.Errorf("period: got %d, want 30", res.Offers[0].Period)
	}
}

func TestLockup_MinimalPeriod_ZeroVol(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.001
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.RegimeParams.Volatility = 0
		c.Config.Period.Min = 2
	})
	res := ls.Apply(ctx)
	// cost = 0.000005 × 2 × 1.0 = 0.00001
	expected := 0.001 + 0.00001
	if !approxEqual(res.Offers[0].Rate, expected, 1e-9) {
		t.Errorf("minimal cost: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

// --- 2.2 Regime amplification ---

func TestLockup_Regime_Crisis(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.005
		c.Snapshot.Regime = domain.RegimeCrisis
		c.Snapshot.RegimeParams.Volatility = 0
		c.Config.Period.Min = 10
		c.Config.Rate.Max = 0.01 // raise max to avoid clamp
	})
	res := ls.Apply(ctx)
	// cost = 0.000005 × 10 × 1.0 × 2.0 = 0.0001
	expected := 0.005 + 0.0001
	if !approxEqual(res.Offers[0].Rate, expected, 1e-9) {
		t.Errorf("crisis: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestLockup_Regime_Backwardation(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.005
		c.Snapshot.Regime = domain.RegimeBackwardation
		c.Snapshot.RegimeParams.Volatility = 0
		c.Config.Period.Min = 10
		c.Config.Rate.Max = 0.01
	})
	res := ls.Apply(ctx)
	// cost = 0.000005 × 10 × 1.0 × 1.5 = 0.000075
	expected := 0.005 + 0.000075
	if !approxEqual(res.Offers[0].Rate, expected, 1e-9) {
		t.Errorf("backwardation: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestLockup_Regime_Contango(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.005
		c.Snapshot.Regime = domain.RegimeContango
		c.Snapshot.RegimeParams.Volatility = 0
		c.Config.Period.Min = 10
		c.Config.Rate.Max = 0.01
	})
	res := ls.Apply(ctx)
	// cost = 0.000005 × 10 × 1.0 × 1.0 = 0.00005
	expected := 0.005 + 0.00005
	if !approxEqual(res.Offers[0].Rate, expected, 1e-9) {
		t.Errorf("contango: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

// --- 2.3 Rate premium output ---

func TestLockup_RatePremium(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00025
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.RegimeParams.Volatility = 0.05
		c.Config.Period.Min = 20
	})
	res := ls.Apply(ctx)
	// cost = 0.000005 × 20 × (1 + 5.0 × 0.05) = 0.0001 × 1.25 = 0.000125
	// rate = 0.00025 + 0.000125 = 0.000375
	expected := 0.00025 + 0.000125
	if !approxEqual(res.Offers[0].Rate, expected, 1e-9) {
		t.Errorf("rate premium: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

func TestLockup_FRRZero_FallbackToConfigMin(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.RegimeParams.Volatility = 0
		c.Config.Rate.Min = 0.0002
		c.Config.Period.Min = 2
	})
	res := ls.Apply(ctx)
	// base = config.Rate.Min = 0.0002
	// cost = 0.000005 × 2 × 1.0 = 0.00001
	// rate = 0.0002 + 0.00001 = 0.00021
	expected := 0.0002 + 0.00001
	if !approxEqual(res.Offers[0].Rate, expected, 1e-9) {
		t.Errorf("FRR zero fallback: got %f, want %f", res.Offers[0].Rate, expected)
	}
}

// --- 2.4 Period shortening ---

func TestLockup_PeriodShortening_ExceedsThreshold(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0001 // low rate
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.RegimeParams.Volatility = 0.10
		c.Config.Period.Min = 60 // long period
		c.Config.Period.Max = 120
	})
	// Original cost = 0.000005 × 60 × 1.5 = 0.00045
	// cost/rate = 0.00045 / 0.0001 = 4.5 → way over 20%
	// Suggested period = 0.20 × 0.0001 / (0.000005 × 1.5 × 1.0) = 0.00002 / 0.0000075 = 2.67 → floor = 2
	// But clamped to config.Period.Min=60? No, clampInt(2, 60, 120) = 60
	res := ls.Apply(ctx)
	if res.Offers[0].Period != 60 {
		t.Errorf("period shortening clamped: got %d, want 60 (config min)", res.Offers[0].Period)
	}
}

func TestLockup_PeriodShortening_SlightlyOver(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0005
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.RegimeParams.Volatility = 0.0
		c.Config.Period.Min = 30
		c.Config.Period.Max = 120
	})
	// cost = 0.000005 × 30 × 1.0 = 0.00015
	// cost/rate = 0.00015 / 0.0005 = 0.30 → over 20%
	// suggested = 0.20 × 0.0005 / (0.000005 × 1.0 × 1.0) = 0.0001 / 0.000005 = 20
	// clamp(20, 30, 120) = 30
	res := ls.Apply(ctx)
	if res.Offers[0].Period != 30 {
		t.Errorf("slightly over: got %d, want 30", res.Offers[0].Period)
	}
}

func TestLockup_PeriodNoShortening(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.005 // high rate, cost will be small fraction
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.RegimeParams.Volatility = 0
		c.Config.Period.Min = 10
	})
	// cost = 0.000005 × 10 × 1.0 = 0.00005
	// cost/rate = 0.00005 / 0.005 = 0.01 → 1%, well under 20%
	res := ls.Apply(ctx)
	if res.Offers[0].Period != 10 {
		t.Errorf("no shortening: got %d, want 10", res.Offers[0].Period)
	}
}

// --- 2.5 Guards ---

func TestLockup_FlashFreeze(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})
	res := ls.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("flash freeze: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "flash_freeze" {
		t.Errorf("reason: got %q, want %q", res.Reason, "flash_freeze")
	}
}

func TestLockup_InsufficientBalance(t *testing.T) {
	ls := NewLockupStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 10
	})
	res := ls.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("low balance: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "insufficient_balance" {
		t.Errorf("reason: got %q, want %q", res.Reason, "insufficient_balance")
	}
}
