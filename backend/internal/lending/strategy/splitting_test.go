package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- 2.1 Depth-aware max order size ---

func TestSplitting_NormalDepth(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 200000
		c.Snapshot.OrderBook.AskDepth = 1000000 // max = 50000
		c.Snapshot.FRR = 0.0003
		c.Config.Amount.Max = 500000
	})
	res := ss.Apply(ctx)
	// 200000 / 50000 = 4 splits
	if len(res.Offers) != 4 {
		t.Fatalf("normal depth: expected 4 offers, got %d", len(res.Offers))
	}
	if !approxEqual(res.Offers[0].Amount, 50000, 0.01) {
		t.Errorf("per split: got %f, want 50000", res.Offers[0].Amount)
	}
}

func TestSplitting_ZeroDepth_NoSplit(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 200000
		c.Snapshot.OrderBook.AskDepth = 0
		c.Snapshot.FRR = 0.0003
		c.Config.Amount.Max = 500000
	})
	res := ss.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("zero depth: expected 1 offer, got %d", len(res.Offers))
	}
	if res.Offers[0].Amount != 200000 {
		t.Errorf("amount: got %f, want 200000", res.Offers[0].Amount)
	}
	if res.Reason != "splitting:no_split" {
		t.Errorf("reason: got %q, want %q", res.Reason, "splitting:no_split")
	}
}

// --- 2.2 Split vs no split ---

func TestSplitting_AmountWithinMax_NoSplit(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 3000
		c.Snapshot.OrderBook.AskDepth = 100000 // max = 5000
		c.Snapshot.FRR = 0.0003
	})
	res := ss.Apply(ctx)
	// 3000 <= 5000, no split
	if len(res.Offers) != 1 {
		t.Fatalf("within max: expected 1 offer, got %d", len(res.Offers))
	}
}

func TestSplitting_AmountExceedsMax(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 8000
		c.Snapshot.OrderBook.AskDepth = 100000 // max = 5000
		c.Snapshot.FRR = 0.0003
	})
	res := ss.Apply(ctx)
	// 8000 / 5000 = ceil(1.6) = 2 splits
	if len(res.Offers) != 2 {
		t.Fatalf("exceeds max: expected 2 offers, got %d", len(res.Offers))
	}
	if !approxEqual(res.Offers[0].Amount, 4000, 0.01) {
		t.Errorf("per split: got %f, want 4000", res.Offers[0].Amount)
	}
}

// --- 2.3 Minimum amount protection ---

func TestSplitting_MinAmountProtection(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 80
		c.Snapshot.OrderBook.AskDepth = 1000 // max = 50
		c.Snapshot.FRR = 0.0003
	})
	// 80 / 50 = ceil(1.6) = 2, but 80/2=40 < 50 → reduce to 1
	res := ss.Apply(ctx)
	if len(res.Offers) != 1 {
		t.Fatalf("min amount: expected 1 offer, got %d", len(res.Offers))
	}
	if res.Offers[0].Amount != 80 {
		t.Errorf("amount: got %f, want 80", res.Offers[0].Amount)
	}
}

func TestSplitting_ExactlyMinAmount(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 200
		c.Snapshot.OrderBook.AskDepth = 1000 // max = 50
		c.Snapshot.FRR = 0.0003
	})
	// 200 / 50 = 4, each = 50 (exactly min)
	res := ss.Apply(ctx)
	if len(res.Offers) != 4 {
		t.Fatalf("exact min: expected 4 offers, got %d", len(res.Offers))
	}
	if !approxEqual(res.Offers[0].Amount, 50, 0.01) {
		t.Errorf("per split: got %f, want 50", res.Offers[0].Amount)
	}
}

// --- 2.4 Max split count cap ---

func TestSplitting_MaxSplitCount(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 1000000
		c.Snapshot.OrderBook.AskDepth = 1000000 // max = 50000
		c.Snapshot.FRR = 0.0003
		c.Config.Amount.Max = 2000000
	})
	// 1000000 / 50000 = 20 → capped at 5
	res := ss.Apply(ctx)
	if len(res.Offers) != 5 {
		t.Fatalf("max splits: expected 5 offers, got %d", len(res.Offers))
	}
	if !approxEqual(res.Offers[0].Amount, 200000, 0.01) {
		t.Errorf("per split: got %f, want 200000", res.Offers[0].Amount)
	}
}

// --- 2.5 Rate and period preserved ---

func TestSplitting_PreservesRateAndPeriod(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 15000
		c.Snapshot.OrderBook.AskDepth = 100000 // max = 5000
		c.Snapshot.FRR = 0.0003
		c.Config.Period.Min = 15
		c.Config.Amount.Max = 50000
	})
	res := ss.Apply(ctx)
	// 15000 / 5000 = 3 splits
	if len(res.Offers) != 3 {
		t.Fatalf("expected 3, got %d", len(res.Offers))
	}
	for i, o := range res.Offers {
		if o.Rate != 0.0003 {
			t.Errorf("offer[%d] rate: got %f, want 0.0003", i, o.Rate)
		}
		if o.Period != 15 {
			t.Errorf("offer[%d] period: got %d, want 15", i, o.Period)
		}
	}
}

// --- 2.6 Guards ---

func TestSplitting_FlashFreeze(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})
	res := ss.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("flash freeze: expected 0, got %d", len(res.Offers))
	}
	if res.Reason != "flash_freeze" {
		t.Errorf("reason: got %q", res.Reason)
	}
}

func TestSplitting_InsufficientBalance(t *testing.T) {
	ss := NewSplittingStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 20
	})
	res := ss.Apply(ctx)
	if len(res.Offers) != 0 {
		t.Errorf("low balance: expected 0, got %d", len(res.Offers))
	}
	if res.Reason != "insufficient_balance" {
		t.Errorf("reason: got %q", res.Reason)
	}
}
