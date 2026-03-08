package strategy

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestPartial_ResidualDetection(t *testing.T) {
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 30},  // residual → cancel
			{ID: 2, Amount: 500}, // normal → keep
			{ID: 3, Amount: 10},  // residual → cancel
		}
	})

	r := s.Apply(ctx)

	if len(r.Cancels) != 2 {
		t.Fatalf("cancels: got %d, want 2", len(r.Cancels))
	}
	if r.Cancels[0] != 1 || r.Cancels[1] != 3 {
		t.Errorf("cancel ids: got %v, want [1 3]", r.Cancels)
	}
}

func TestPartial_NormalOfferNotCancelled(t *testing.T) {
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 500},
			{ID: 2, Amount: 1000},
		}
	})

	r := s.Apply(ctx)

	if len(r.Cancels) != 0 {
		t.Errorf("cancels: got %d, want 0", len(r.Cancels))
	}
}

func TestPartial_LowActivity(t *testing.T) {
	// offers avg=9000, Amount.Max=10000 → fillProxy = 1 - 9000/10000 = 0.1 → low
	// amount = 5000 × 0.80 = 4000
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 9000},
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "partial:low_activity" {
		t.Fatalf("reason: got %s, want partial:low_activity", r.Reason)
	}
	// amount = min(5000, 10000) × 0.80 = 4000
	if !approxEqual(r.Offers[0].Amount, 4000, 1e-6) {
		t.Errorf("amount: got %f, want 4000", r.Offers[0].Amount)
	}
}

func TestPartial_NormalActivity(t *testing.T) {
	// offers avg=5000, Amount.Max=10000 → fillProxy = 1 - 5000/10000 = 0.5 → normal
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 5000},
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "partial:normal" {
		t.Fatalf("reason: got %s, want partial:normal", r.Reason)
	}
	if !approxEqual(r.Offers[0].Amount, 5000, 1e-6) {
		t.Errorf("amount: got %f, want 5000 (unchanged)", r.Offers[0].Amount)
	}
}

func TestPartial_HighActivity(t *testing.T) {
	// offers avg=2000, Amount.Max=50000 → fillProxy = 1 - 2000/50000 = 0.96 → high
	// base = min(5000, 50000) = 5000, × 1.20 = 6000, cap=50000, avail=50000 → 6000
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 50000
		c.Config.Amount.Max = 50000
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 2000},
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "partial:high_activity" {
		t.Fatalf("reason: got %s, want partial:high_activity", r.Reason)
	}
	// base = min(50000, 50000) = 50000, × 1.20 = 60000, cap=50000 → 50000
	if !approxEqual(r.Offers[0].Amount, 50000, 1e-6) {
		t.Errorf("amount: got %f, want 50000", r.Offers[0].Amount)
	}
}

func TestPartial_HighActivityCapped(t *testing.T) {
	// available=9000, Amount.Max=10000 → base amount=9000
	// fillProxy high → 9000 × 1.20 = 10800 → capped at 10000
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Available = 9000
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 2000},
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "partial:high_activity" {
		t.Fatalf("reason: got %s, want partial:high_activity", r.Reason)
	}
	// 9000 × 1.20 = 10800 → min(10800, 10000) = 10000 → min(10000, 9000) = 9000
	if !approxEqual(r.Offers[0].Amount, 9000, 1e-6) {
		t.Errorf("amount: got %f, want 9000 (capped by available)", r.Offers[0].Amount)
	}
}

func TestPartial_NoActiveOffers(t *testing.T) {
	// No offers → fillProxy=0 → low activity
	s := NewPartialStrategy()
	ctx := testCtx()

	r := s.Apply(ctx)

	if r.Reason != "partial:low_activity" {
		t.Fatalf("reason: got %s, want partial:low_activity", r.Reason)
	}
	// amount = 5000 × 0.80 = 4000
	if !approxEqual(r.Offers[0].Amount, 4000, 1e-6) {
		t.Errorf("amount: got %f, want 4000", r.Offers[0].Amount)
	}
}

func TestPartial_FlashFreeze(t *testing.T) {
	s := NewPartialStrategy()
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

func TestPartial_InsufficientBalance(t *testing.T) {
	s := NewPartialStrategy()
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

func TestPartial_ResidualExcludedFromFillProxy(t *testing.T) {
	// residual (30) excluded from avg, valid offer avg=8000 → fillProxy = 0.2 → low
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 30},   // residual
			{ID: 2, Amount: 8000}, // valid
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "partial:low_activity" {
		t.Fatalf("reason: got %s, want partial:low_activity", r.Reason)
	}
	if len(r.Cancels) != 1 {
		t.Errorf("cancels: got %d, want 1", len(r.Cancels))
	}
}

func TestPartial_FillProxyMultipleOffers(t *testing.T) {
	// offers: [3000, 5000, 2000] → avg=3333.33 → fillProxy = 1 - 3333.33/10000 = 0.667 → normal
	s := NewPartialStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveOffers = []domain.FundingOffer{
			{ID: 1, Amount: 3000},
			{ID: 2, Amount: 5000},
			{ID: 3, Amount: 2000},
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "partial:normal" {
		t.Fatalf("reason: got %s, want partial:normal", r.Reason)
	}
}
