package strategy

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- 2.1 Regime-driven base period ---

func TestPeriod_Regime_Contango(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeContango
		c.Snapshot.FRR = 0 // disable rate scaling
	})
	// Period.Min=2, Period.Max=30, range=28
	// contango: 2 + 0.88 * 28 = 26.64 → round to 27
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 27 {
		t.Errorf("contango period: got %d, want 27", res.Offers[0].Period)
	}
}

func TestPeriod_Regime_Neutral(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0
	})
	// 2 + 0.50 * 28 = 16
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 16 {
		t.Errorf("neutral period: got %d, want 16", res.Offers[0].Period)
	}
}

func TestPeriod_Regime_Backwardation(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeBackwardation
		c.Snapshot.FRR = 0
	})
	// 2 + 0.25 * 28 = 9
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 9 {
		t.Errorf("backwardation period: got %d, want 9", res.Offers[0].Period)
	}
}

func TestPeriod_Regime_Crisis(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeCrisis
		c.Snapshot.FRR = 0
	})
	// crisis: 2 + 0.0 * 28 = 2
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 2 {
		t.Errorf("crisis period: got %d, want 2", res.Offers[0].Period)
	}
}

// --- 2.2 Rate-based scaling ---

func TestPeriod_RateScaling_AboveFRR(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0.0001
		c.Config.Rate.Min = 0.00015 // rate/FRR = 1.5
	})
	// base = 2 + 0.50 * 28 = 16
	// scale = 1.0 + (1.5 - 1.0) * 0.3 = 1.15
	// 16 * 1.15 = 18.4 → round = 18
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 18 {
		t.Errorf("rate above FRR: got %d, want 18", res.Offers[0].Period)
	}
}

func TestPeriod_RateScaling_BelowFRR(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0.0002
		c.Config.Rate.Min = 0.00016 // rate/FRR = 0.8
	})
	// base = 16
	// scale = 1.0 + (0.8 - 1.0) * 0.3 = 0.94
	// 16 * 0.94 = 15.04 → round = 15
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 15 {
		t.Errorf("rate below FRR: got %d, want 15", res.Offers[0].Period)
	}
}

func TestPeriod_RateScaling_FRRZero(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0
	})
	// FRR=0 → skip rate scaling → base = 16
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 16 {
		t.Errorf("FRR zero: got %d, want 16", res.Offers[0].Period)
	}
}

// --- 2.3 Volatility discount ---

func TestPeriod_Volatility_Normal(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0
		c.Snapshot.RegimeParams.Volatility = 0.05 // below threshold
	})
	// No discount → 16
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 16 {
		t.Errorf("normal vol: got %d, want 16", res.Offers[0].Period)
	}
}

func TestPeriod_Volatility_High(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0
		c.Snapshot.RegimeParams.Volatility = 0.20
	})
	// discount = 1.0 - (0.20 - 0.10) * 2.0 = 0.80
	// 16 * 0.80 = 12.8 → round = 13
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 13 {
		t.Errorf("high vol: got %d, want 13", res.Offers[0].Period)
	}
}

func TestPeriod_Volatility_Extreme(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0
		c.Snapshot.RegimeParams.Volatility = 0.50 // way above cap
	})
	// excess capped at 0.20, discount = 1.0 - 0.20 * 2.0 = 0.60
	// 16 * 0.60 = 9.6 → round = 10
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 10 {
		t.Errorf("extreme vol: got %d, want 10", res.Offers[0].Period)
	}
}

// --- 2.4 Clamp and rounding ---

func TestPeriod_Clamp_BelowMin(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeCrisis
		c.Snapshot.FRR = 0
		c.Snapshot.RegimeParams.Volatility = 0.50 // heavy discount
		c.Config.Period.Min = 5
		c.Config.Period.Max = 30
	})
	// crisis: 5 + 0.0 * 25 = 5, vol discount → 5 * 0.60 = 3.0 → round 3 → clamp to 5
	res := ps.Apply(ctx)
	if res.Offers[0].Period != 5 {
		t.Errorf("clamp below min: got %d, want 5", res.Offers[0].Period)
	}
}

func TestPeriod_Clamp_AboveMax(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeContango
		c.Snapshot.FRR = 0.00005
		c.Config.Rate.Min = 0.0001 // ratio = 2.0 (capped)
		c.Config.Period.Min = 2
		c.Config.Period.Max = 10
	})
	// contango: 2 + 0.88 * 8 = 9.04
	// rate scale: 1.0 + (2.0 - 1.0) * 0.3 = 1.30
	// 9.04 * 1.30 = 11.75 → round 12 → clamp to max=10
	res := ps.Apply(ctx)
	if res.Offers[0].Period > 10 {
		t.Errorf("clamp above max: got %d, want <= 10", res.Offers[0].Period)
	}
}

func TestPeriod_Rounding(t *testing.T) {
	ps := NewPeriodStrategy()
	// Test that we get proper rounding (not truncation)
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Regime = domain.RegimeNeutral
		c.Snapshot.FRR = 0
		c.Snapshot.RegimeParams.Volatility = 0.20
	})
	// base=16, vol discount=0.80, result=12.8 → should round to 13 not truncate to 12
	res := ps.Apply(ctx)
	expected := int(math.Round(16.0 * 0.80))
	if res.Offers[0].Period != expected {
		t.Errorf("rounding: got %d, want %d", res.Offers[0].Period, expected)
	}
}

// --- 2.5 Flash freeze and insufficient balance ---

func TestPeriod_FlashFreeze(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})
	res := ps.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("flash freeze: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "flash_freeze" {
		t.Errorf("reason: got %q, want %q", res.Reason, "flash_freeze")
	}
}

func TestPeriod_InsufficientBalance(t *testing.T) {
	ps := NewPeriodStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 20
	})
	res := ps.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("low balance: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "insufficient_balance" {
		t.Errorf("reason: got %q, want %q", res.Reason, "insufficient_balance")
	}
}

// --- Period Ladder Tests ---

func ladderCfg() *domain.StrategyConfig {
	return &domain.StrategyConfig{
		Currency: "USD",
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
		Rate:     domain.RateConfig{Min: 0.0001, Max: 0.01},
		Amount:   domain.AmountConfig{Min: 50, Max: 10000},
	}
}

func TestComputePeriodLadder_ThreeTiers(t *testing.T) {
	cfg := ladderCfg()
	now := time.Now()

	ladder := ComputePeriodLadder(
		domain.RegimeNeutral, 0.05, 0.0002, 0.0002, cfg,
		0.0, nil, 3, now,
	)

	if len(ladder.Periods) != 3 {
		t.Fatalf("expected 3 periods, got %d", len(ladder.Periods))
	}

	// Short=[2,11], Medium=[12,20], Long=[21,30]
	if ladder.Periods[0] < 2 || ladder.Periods[0] > 11 {
		t.Errorf("short period %d not in [2,11]", ladder.Periods[0])
	}
	if ladder.Periods[1] < 12 || ladder.Periods[1] > 20 {
		t.Errorf("medium period %d not in [12,20]", ladder.Periods[1])
	}
	if ladder.Periods[2] < 21 || ladder.Periods[2] > 30 {
		t.Errorf("long period %d not in [21,30]", ladder.Periods[2])
	}
}

func TestComputePeriodLadder_Degrade(t *testing.T) {
	cfg := &domain.StrategyConfig{
		Currency: "USD",
		Period:   domain.PeriodConfig{Min: 2, Max: 4}, // range=2 < 3
		Rate:     domain.RateConfig{Min: 0.0001, Max: 0.01},
		Amount:   domain.AmountConfig{Min: 50, Max: 10000},
	}
	now := time.Now()

	ladder := ComputePeriodLadder(
		domain.RegimeNeutral, 0.05, 0.0002, 0.0002, cfg,
		0.0, nil, 3, now,
	)

	// Should degrade: all periods same
	if len(ladder.Periods) != 3 {
		t.Fatalf("expected 3 periods, got %d", len(ladder.Periods))
	}
	if ladder.Periods[0] != ladder.Periods[1] || ladder.Periods[1] != ladder.Periods[2] {
		t.Errorf("degrade: periods should be identical, got %v", ladder.Periods)
	}
}

func TestComputePeriodLadder_RatePercentileShift(t *testing.T) {
	cfg := ladderCfg()
	now := time.Now()

	ladderHigh := ComputePeriodLadder(
		domain.RegimeNeutral, 0.05, 0.0002, 0.0002, cfg,
		0.8, nil, 3, now,
	)
	ladderLow := ComputePeriodLadder(
		domain.RegimeNeutral, 0.05, 0.0002, 0.0002, cfg,
		-0.8, nil, 3, now,
	)

	// High percentile should produce longer periods than low
	for i := 0; i < 3; i++ {
		if ladderHigh.Periods[i] < ladderLow.Periods[i] {
			t.Errorf("tier %d: high percentile (%d) should >= low percentile (%d)",
				i, ladderHigh.Periods[i], ladderLow.Periods[i])
		}
	}
}

func TestComputePeriodLadder_SingleTier(t *testing.T) {
	cfg := ladderCfg()
	now := time.Now()

	ladder := ComputePeriodLadder(
		domain.RegimeNeutral, 0.05, 0.0002, 0.0002, cfg,
		0.0, nil, 1, now,
	)

	if len(ladder.Periods) != 1 {
		t.Fatalf("expected 1 period, got %d", len(ladder.Periods))
	}
	// Single tier uses ComputePeriod fallback
	expected := ComputePeriod(domain.RegimeNeutral, 0.05, 0.0002, 0.0002, cfg)
	if ladder.Periods[0] != expected {
		t.Errorf("single tier: got %d, want %d", ladder.Periods[0], expected)
	}
}
