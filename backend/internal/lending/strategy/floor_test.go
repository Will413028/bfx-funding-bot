package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- 2.1 Opportunity cost floor ---

func TestFloor_OpportunityCost_Baseline(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00005 // FRR floor = 0.00004, below config.Rate.Min
		c.Snapshot.OrderBook.MidRate = 0.00005
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	// config.Rate.Min = 0.0001, FRR floor = 0.00004, regime floor = 0.0001
	// max(0.0001, 0.00004, 0.0001) = 0.0001
	res := fs.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("expected 1 offer, got %d", len(res.Offers))
	}
	if res.Offers[0].Rate != 0.0001 {
		t.Errorf("opportunity cost floor: got %f, want 0.0001", res.Offers[0].Rate)
	}
	if res.Reason != "floor:opportunity_cost" {
		t.Errorf("reason: got %q, want %q", res.Reason, "floor:opportunity_cost")
	}
}

// --- 2.2 FRR relative floor ---

func TestFloor_FRR_Available(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0005 // FRR floor = 0.00046, above config.Rate.Min=0.0001
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	res := fs.Apply(ctx)
	// FRR floor = 0.0005 * 0.92 = 0.00046
	expected := 0.0005 * 0.92
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("FRR floor: got %f, want %f", res.Offers[0].Rate, expected)
	}
	if res.Reason != "floor:frr_relative" {
		t.Errorf("reason: got %q, want %q", res.Reason, "floor:frr_relative")
	}
}

func TestFloor_FRR_Zero(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0 // FRR floor = 0
		c.Snapshot.OrderBook.MidRate = 0
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	res := fs.Apply(ctx)
	// FRR floor = 0, so opportunity cost (0.0001) wins
	if res.Offers[0].Rate != 0.0001 {
		t.Errorf("FRR zero: got %f, want 0.0001", res.Offers[0].Rate)
	}
}

// --- 2.3 Regime adjustment ---

func TestFloor_Regime_Crisis(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00005 // FRR floor = 0.00004
		c.Snapshot.OrderBook.MidRate = 0.00005
		c.Snapshot.Regime = domain.RegimeCrisis
	})
	res := fs.Apply(ctx)
	// regime floor = 0.0001 * 1.5 = 0.00015
	// max(0.0001, 0.00004, 0.00015) = 0.00015
	expected := 0.0001 * 1.5
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("crisis floor: got %f, want %f", res.Offers[0].Rate, expected)
	}
	if res.Reason != "floor:regime" {
		t.Errorf("reason: got %q, want %q", res.Reason, "floor:regime")
	}
}

func TestFloor_Regime_Backwardation(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00005 // FRR floor = 0.00004
		c.Snapshot.OrderBook.MidRate = 0.00005
		c.Snapshot.Regime = domain.RegimeBackwardation
	})
	res := fs.Apply(ctx)
	// regime floor = 0.0001 * 1.2 = 0.00012
	expected := 0.0001 * 1.2
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("backwardation floor: got %f, want %f", res.Offers[0].Rate, expected)
	}
	if res.Reason != "floor:regime" {
		t.Errorf("reason: got %q, want %q", res.Reason, "floor:regime")
	}
}

func TestFloor_Regime_Contango(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00005 // FRR floor = 0.00004
		c.Snapshot.OrderBook.MidRate = 0.00005
		c.Snapshot.Regime = domain.RegimeContango
	})
	res := fs.Apply(ctx)
	// regime floor = 0.0001 * 1.0, same as opportunity cost
	if res.Offers[0].Rate != 0.0001 {
		t.Errorf("contango floor: got %f, want 0.0001", res.Offers[0].Rate)
	}
}

func TestFloor_Regime_Neutral(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.00005
		c.Snapshot.OrderBook.MidRate = 0.00005
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	res := fs.Apply(ctx)
	if res.Offers[0].Rate != 0.0001 {
		t.Errorf("neutral floor: got %f, want 0.0001", res.Offers[0].Rate)
	}
}

// --- 2.4 Three-layer max ---

func TestFloor_MaxLayer_FRRWins(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.001 // FRR floor = 0.00092
		c.Snapshot.Regime = domain.RegimeNeutral
		// opportunity cost = 0.0001, regime = 0.0001
	})
	res := fs.Apply(ctx)
	expected := 0.001 * 0.92
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("FRR wins: got %f, want %f", res.Offers[0].Rate, expected)
	}
	if res.Reason != "floor:frr_relative" {
		t.Errorf("reason: got %q, want %q", res.Reason, "floor:frr_relative")
	}
}

func TestFloor_MaxLayer_RegimeWins(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Config.Rate.Min = 0.0003 // opportunity cost = 0.0003
		c.Snapshot.FRR = 0.0003    // FRR floor = 0.000276
		c.Snapshot.Regime = domain.RegimeCrisis
		// regime floor = 0.0003 * 1.5 = 0.00045
	})
	res := fs.Apply(ctx)
	expected := 0.0003 * 1.5
	if !approxEqual(res.Offers[0].Rate, expected, 1e-10) {
		t.Errorf("regime wins: got %f, want %f", res.Offers[0].Rate, expected)
	}
	if res.Reason != "floor:regime" {
		t.Errorf("reason: got %q, want %q", res.Reason, "floor:regime")
	}
}

func TestFloor_MaxLayer_OpportunityCostWins(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Config.Rate.Min = 0.0005 // opportunity cost = 0.0005
		c.Snapshot.FRR = 0.0004    // FRR floor = 0.000368
		c.Snapshot.Regime = domain.RegimeNeutral
		// regime floor = 0.0005 * 1.0 = 0.0005, ties with opportunity cost
	})
	res := fs.Apply(ctx)
	// opportunity cost and regime are equal, opportunity cost is checked first
	if res.Offers[0].Rate != 0.0005 {
		t.Errorf("opportunity cost wins: got %f, want 0.0005", res.Offers[0].Rate)
	}
}

// --- 2.5 Dominant source reason ---

func TestFloor_Reason_Tracking(t *testing.T) {
	fs := NewFloorStrategy()

	// Case 1: opportunity cost wins
	ctx1 := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0
		c.Snapshot.OrderBook.MidRate = 0
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	if r := fs.Apply(ctx1); r.Reason != "floor:opportunity_cost" {
		t.Errorf("case 1 reason: got %q", r.Reason)
	}

	// Case 2: FRR wins
	ctx2 := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.001
		c.Snapshot.Regime = domain.RegimeNeutral
	})
	if r := fs.Apply(ctx2); r.Reason != "floor:frr_relative" {
		t.Errorf("case 2 reason: got %q", r.Reason)
	}

	// Case 3: regime wins
	ctx3 := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0
		c.Snapshot.OrderBook.MidRate = 0
		c.Snapshot.Regime = domain.RegimeCrisis
	})
	if r := fs.Apply(ctx3); r.Reason != "floor:regime" {
		t.Errorf("case 3 reason: got %q", r.Reason)
	}
}

// --- 2.6 Flash freeze ---

func TestFloor_FlashFreeze(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})
	res := fs.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("flash freeze: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "flash_freeze" {
		t.Errorf("reason: got %q, want %q", res.Reason, "flash_freeze")
	}
}

// --- 2.7 Insufficient balance ---

func TestFloor_InsufficientBalance(t *testing.T) {
	fs := NewFloorStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 10
	})
	res := fs.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("low balance: expected 0 offers, got %d", len(res.Offers))
	}
	if res.Reason != "insufficient_balance" {
		t.Errorf("reason: got %q, want %q", res.Reason, "insufficient_balance")
	}
}
