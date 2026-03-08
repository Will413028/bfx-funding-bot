package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestImpact_LowImpact(t *testing.T) {
	// existing=0, new=5000, askDepth=100000 → ratio=5% → no adjustment
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
	})

	r := s.Apply(ctx)

	if r.Reason != "impact:low" {
		t.Fatalf("reason: got %s, want impact:low", r.Reason)
	}
	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	if r.Offers[0].Amount != 5000 {
		t.Errorf("amount: got %f, want 5000", r.Offers[0].Amount)
	}
	if r.Offers[0].Rate != 0.00025 {
		t.Errorf("rate: got %f, want 0.00025 (unchanged)", r.Offers[0].Rate)
	}
}

func TestImpact_LowImpactWithExisting(t *testing.T) {
	// existing=5000, new=5000, askDepth=100000 → ratio=10% → still low (<=10%)
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.ActiveOffers = []domain.FundingOffer{{Amount: 5000}}
	})

	r := s.Apply(ctx)

	if r.Reason != "impact:low" {
		t.Fatalf("reason: got %s, want impact:low", r.Reason)
	}
}

func TestImpact_ModerateImpact(t *testing.T) {
	// existing=0, new=5000, askDepth=30000 → ratio=16.7% → moderate (+3%)
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.Snapshot.OrderBook.AskDepth = 30000
	})

	r := s.Apply(ctx)

	if r.Reason != "impact:moderate" {
		t.Fatalf("reason: got %s, want impact:moderate", r.Reason)
	}
	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	expectedRate := 0.00025 * 1.03
	if !approxEqual(r.Offers[0].Rate, expectedRate, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expectedRate)
	}
	if r.Offers[0].Amount != 5000 {
		t.Errorf("amount: got %f, want 5000 (unchanged)", r.Offers[0].Amount)
	}
}

func TestImpact_ModerateWithExisting(t *testing.T) {
	// existing=20000, new=10000, askDepth=200000 → ratio=15% → moderate
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 10000
		c.Snapshot.OrderBook.AskDepth = 200000
		c.ActiveOffers = []domain.FundingOffer{{Amount: 20000}}
	})

	r := s.Apply(ctx)

	if r.Reason != "impact:moderate" {
		t.Fatalf("reason: got %s, want impact:moderate", r.Reason)
	}
}

func TestImpact_HighImpact(t *testing.T) {
	// existing=30000, new=10000(capped), askDepth=200000 → ratio=20000/200000=25%+ → high
	// maxAllowed = 25%×200000 - 30000 = 20000
	// available=50000, amount=min(50000,10000)=10000, but 10000<20000 so reducedAmount=10000
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 50000
		c.Config.Amount.Max = 50000
		c.Snapshot.OrderBook.AskDepth = 100000
		c.ActiveOffers = []domain.FundingOffer{{Amount: 30000}}
	})
	// existing=30000, new=50000, askDepth=100000 → ratio=80% → high
	// maxAllowed = 25%×100000 - 30000 = -5000 → <50 → over_limit
	// Let's adjust: existing=10000, askDepth=100000 → ratio=(10000+50000)/100000=60% → high
	// maxAllowed = 25000-10000=15000

	ctx.ActiveOffers = []domain.FundingOffer{{Amount: 10000}}

	r := s.Apply(ctx)

	if r.Reason != "impact:high" {
		t.Fatalf("reason: got %s, want impact:high", r.Reason)
	}
	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	// maxAllowed = 25%×100000 - 10000 = 15000
	// reducedAmount = min(50000, 15000) = 15000
	if !approxEqual(r.Offers[0].Amount, 15000, 1e-6) {
		t.Errorf("amount: got %f, want 15000", r.Offers[0].Amount)
	}
	expectedRate := clamp(0.00025*1.05, 0.0001, 0.005)
	if !approxEqual(r.Offers[0].Rate, expectedRate, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expectedRate)
	}
}

func TestImpact_OverLimit(t *testing.T) {
	// existing=26000, askDepth=100000 → maxAllowed=25000-26000=-1000 → over_limit
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 5000
		c.Snapshot.OrderBook.AskDepth = 100000
		c.ActiveOffers = []domain.FundingOffer{{Amount: 26000}}
	})

	r := s.Apply(ctx)

	if r.Reason != "impact:over_limit" {
		t.Fatalf("reason: got %s, want impact:over_limit", r.Reason)
	}
	if len(r.Offers) != 0 {
		t.Errorf("offers: got %d, want 0", len(r.Offers))
	}
}

func TestImpact_ZeroDepth(t *testing.T) {
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.OrderBook.AskDepth = 0
	})

	r := s.Apply(ctx)

	if r.Reason != "impact:no_data" {
		t.Fatalf("reason: got %s, want impact:no_data", r.Reason)
	}
	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
}

func TestImpact_FlashFreeze(t *testing.T) {
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})

	r := s.Apply(ctx)

	if r.Reason != "flash_freeze" {
		t.Fatalf("reason: got %s, want flash_freeze", r.Reason)
	}
	if len(r.Offers) != 0 {
		t.Errorf("offers: got %d, want 0", len(r.Offers))
	}
}

func TestImpact_InsufficientBalance(t *testing.T) {
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 10
	})

	r := s.Apply(ctx)

	if r.Reason != "insufficient_balance" {
		t.Fatalf("reason: got %s, want insufficient_balance", r.Reason)
	}
	if len(r.Offers) != 0 {
		t.Errorf("offers: got %d, want 0", len(r.Offers))
	}
}

func TestImpact_RateClampedToMax(t *testing.T) {
	// High FRR × 1.05 would exceed Rate.Max → should be clamped
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0049
		c.Config.Rate.Max = 0.005
		c.Available = 5000
		c.Snapshot.OrderBook.AskDepth = 10000
		c.ActiveOffers = []domain.FundingOffer{{Amount: 3000}}
		// ratio = (3000+5000)/10000 = 80% → high
		// maxAllowed = 2500-3000 = -500 → over_limit
	})
	// Adjust to moderate range instead
	ctx.Snapshot.OrderBook.AskDepth = 30000
	ctx.ActiveOffers = []domain.FundingOffer{{Amount: 1000}}
	// ratio = (1000+5000)/30000 = 20% → moderate
	// rate = 0.0049 × 1.03 = 0.005047 → clamped to 0.005

	r := s.Apply(ctx)

	if r.Reason != "impact:moderate" {
		t.Fatalf("reason: got %s, want impact:moderate", r.Reason)
	}
	if r.Offers[0].Rate != 0.005 {
		t.Errorf("rate: got %f, want 0.005 (clamped)", r.Offers[0].Rate)
	}
}

func TestImpact_FRRFallbackToMin(t *testing.T) {
	// FRR=0 → falls back to config.Rate.Min
	s := NewImpactStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0
		c.Available = 5000
	})

	r := s.Apply(ctx)

	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	if r.Offers[0].Rate != 0.0001 {
		t.Errorf("rate: got %f, want 0.0001 (config min)", r.Offers[0].Rate)
	}
}
