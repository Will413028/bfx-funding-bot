package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- 2.1 Tier count based on balance ---

func TestAllocation_SmallBalance_SingleTier(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 100
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("small balance: expected 1 offer, got %d", len(res.Offers))
	}
	if res.Offers[0].Amount != 100 {
		t.Errorf("amount: got %f, want 100", res.Offers[0].Amount)
	}
}

func TestAllocation_MediumBalance_TwoTiers(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 500
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 2 {
		t.Fatalf("medium balance: expected 2 offers, got %d", len(res.Offers))
	}
}

func TestAllocation_LargeBalance_ThreeTiers(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 3 {
		t.Fatalf("large balance: expected 3 offers, got %d", len(res.Offers))
	}
}

// --- 2.2 Minimum amount protection ---

func TestAllocation_MinAmountProtection_FallbackToSingle(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 120 // 30% = 36, below 50 → fallback to single
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("min amount protection: expected 1 offer, got %d", len(res.Offers))
	}
	if res.Offers[0].Amount != 120 {
		t.Errorf("amount: got %f, want 120", res.Offers[0].Amount)
	}
}

func TestAllocation_MinAmountProtection_ThreeToTwo(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 1200 // 3 tiers: 20% = 240 (ok), but check boundary
		c.Snapshot.FRR = 0.0002
	})
	// 1200 > 1000, three tiers: 600, 360, 240 — all >= 50
	res := as.Apply(ctx)
	if len(res.Offers) != 3 {
		t.Fatalf("three tiers ok: expected 3 offers, got %d", len(res.Offers))
	}
}

// --- 2.3 Rate multipliers ---

func TestAllocation_RateMultipliers_TwoTiers(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 500
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 2 {
		t.Fatalf("expected 2 offers, got %d", len(res.Offers))
	}
	// Core: 0.0002 × 1.0
	if !approxEqual(res.Offers[0].Rate, 0.0002, 1e-10) {
		t.Errorf("core rate: got %f, want 0.0002", res.Offers[0].Rate)
	}
	// Aggressive: 0.0002 × 1.25
	if !approxEqual(res.Offers[1].Rate, 0.00025, 1e-10) {
		t.Errorf("aggressive rate: got %f, want 0.00025", res.Offers[1].Rate)
	}
}

func TestAllocation_RateMultipliers_ThreeTiers(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 3 {
		t.Fatalf("expected 3 offers, got %d", len(res.Offers))
	}
	// Core: 0.0002 × 1.0
	if !approxEqual(res.Offers[0].Rate, 0.0002, 1e-10) {
		t.Errorf("core rate: got %f, want 0.0002", res.Offers[0].Rate)
	}
	// Moderate: 0.0002 × 1.10 = 0.00022
	if !approxEqual(res.Offers[1].Rate, 0.00022, 1e-10) {
		t.Errorf("moderate rate: got %f, want 0.00022", res.Offers[1].Rate)
	}
	// Aggressive: 0.0002 × 1.25 = 0.00025
	if !approxEqual(res.Offers[2].Rate, 0.00025, 1e-10) {
		t.Errorf("aggressive rate: got %f, want 0.00025", res.Offers[2].Rate)
	}
}

// --- 2.4 Regime adjustment ---

func TestAllocation_Regime_Crisis_ForceSingleTier(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.Snapshot.FRR = 0.0002
		c.Snapshot.Regime = domain.RegimeCrisis
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("crisis: expected 1 offer, got %d", len(res.Offers))
	}
	if res.Offers[0].Amount != 5000 {
		t.Errorf("crisis amount: got %f, want 5000", res.Offers[0].Amount)
	}
}

func TestAllocation_Regime_Backwardation_MaxTwo(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.Snapshot.FRR = 0.0002
		c.Snapshot.Regime = domain.RegimeBackwardation
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 2 {
		t.Fatalf("backwardation: expected 2 offers, got %d", len(res.Offers))
	}
}

func TestAllocation_Regime_Contango_AllowThree(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.Snapshot.FRR = 0.0002
		c.Snapshot.Regime = domain.RegimeContango
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 3 {
		t.Fatalf("contango: expected 3 offers, got %d", len(res.Offers))
	}
}

// --- 2.5 Amount allocation ratios ---

func TestAllocation_AmountRatios_TwoTiers(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 1000
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 2 {
		t.Fatalf("expected 2, got %d", len(res.Offers))
	}
	// 70% core, 30% aggressive
	if !approxEqual(res.Offers[0].Amount, 700, 0.01) {
		t.Errorf("core amount: got %f, want 700", res.Offers[0].Amount)
	}
	if !approxEqual(res.Offers[1].Amount, 300, 0.01) {
		t.Errorf("aggressive amount: got %f, want 300", res.Offers[1].Amount)
	}
}

func TestAllocation_AmountRatios_ThreeTiers(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 2000
		c.Snapshot.FRR = 0.0002
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 3 {
		t.Fatalf("expected 3, got %d", len(res.Offers))
	}
	// 50%, 30%, 20%
	if !approxEqual(res.Offers[0].Amount, 1000, 0.01) {
		t.Errorf("core: got %f, want 1000", res.Offers[0].Amount)
	}
	if !approxEqual(res.Offers[1].Amount, 600, 0.01) {
		t.Errorf("moderate: got %f, want 600", res.Offers[1].Amount)
	}
	if !approxEqual(res.Offers[2].Amount, 400, 0.01) {
		t.Errorf("aggressive: got %f, want 400", res.Offers[2].Amount)
	}
}

// --- 2.6 Guards ---

func TestAllocation_FlashFreeze(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("flash freeze: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "flash_freeze" {
		t.Errorf("reason: got %q", res.Reason)
	}
}

func TestAllocation_InsufficientBalance(t *testing.T) {
	as := NewAllocationStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 30
	})
	res := as.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("low balance: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "insufficient_balance" {
		t.Errorf("reason: got %q", res.Reason)
	}
}
