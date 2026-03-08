package strategy

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func newStaggerAt(now time.Time) *StaggerStrategy {
	return &StaggerStrategy{now: func() time.Time { return now }}
}

func TestStagger_FillGap(t *testing.T) {
	// Period range [3, 7], credits expire at day 3, 4, 6, 7 → day 5 is the only gap
	now := time.Date(2026, 3, 9, 0, 0, 0, 0, time.UTC)
	s := newStaggerAt(now)
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Config.Period.Min = 3
		c.Config.Period.Max = 7
		c.ActiveCredits = []domain.FundingCredit{
			{Amount: 1000, Period: 3, OpenedAt: now},
			{Amount: 1000, Period: 4, OpenedAt: now},
			{Amount: 1000, Period: 6, OpenedAt: now},
			{Amount: 1000, Period: 7, OpenedAt: now},
		}
	})

	r := s.Apply(ctx)

	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	if r.Offers[0].Period != 5 {
		t.Errorf("period: got %d, want 5", r.Offers[0].Period)
	}
}

func TestStagger_MultipleEmptyDays(t *testing.T) {
	// Credits expire only at day 2 → days 3-30 all empty
	// Middle of empty days [3..30] → index 14 → day 17
	now := time.Date(2026, 3, 9, 0, 0, 0, 0, time.UTC)
	s := newStaggerAt(now)
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveCredits = []domain.FundingCredit{
			{Amount: 5000, Period: 2, OpenedAt: now},
		}
	})

	r := s.Apply(ctx)

	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	// days 3-30 = 28 empty days, middle index = 14 → day 3+14 = 17
	if r.Offers[0].Period != 17 {
		t.Errorf("period: got %d, want 17 (middle of empty range)", r.Offers[0].Period)
	}
}

func TestStagger_NoCredits(t *testing.T) {
	s := NewStaggerStrategy()
	ctx := testCtx()

	r := s.Apply(ctx)

	if r.Reason != "stagger:no_credits" {
		t.Fatalf("reason: got %s, want stagger:no_credits", r.Reason)
	}
	// midpoint of [2, 30] = 16
	if r.Offers[0].Period != 16 {
		t.Errorf("period: got %d, want 16", r.Offers[0].Period)
	}
}

func TestStagger_Concentrated(t *testing.T) {
	// All credits expire on same day → concentration = 100%
	now := time.Date(2026, 3, 9, 0, 0, 0, 0, time.UTC)
	s := newStaggerAt(now)
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveCredits = []domain.FundingCredit{
			{Amount: 3000, Period: 5, OpenedAt: now},
			{Amount: 2000, Period: 5, OpenedAt: now},
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "stagger:concentrated" {
		t.Fatalf("reason: got %s, want stagger:concentrated", r.Reason)
	}
	// Day 5 is concentrated, should pick another day
	if r.Offers[0].Period == 5 {
		t.Errorf("period: got 5, should avoid concentrated day")
	}
}

func TestStagger_Balanced(t *testing.T) {
	// Credits spread across many days, no single day > 50%
	now := time.Date(2026, 3, 9, 0, 0, 0, 0, time.UTC)
	s := newStaggerAt(now)
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveCredits = []domain.FundingCredit{
			{Amount: 1000, Period: 3, OpenedAt: now},
			{Amount: 1000, Period: 5, OpenedAt: now},
			{Amount: 1000, Period: 10, OpenedAt: now},
			{Amount: 1000, Period: 15, OpenedAt: now},
			{Amount: 1000, Period: 20, OpenedAt: now},
		}
	})

	r := s.Apply(ctx)

	if r.Reason != "stagger:balanced" {
		t.Fatalf("reason: got %s, want stagger:balanced", r.Reason)
	}
}

func TestStagger_FlashFreeze(t *testing.T) {
	s := NewStaggerStrategy()
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

func TestStagger_InsufficientBalance(t *testing.T) {
	s := NewStaggerStrategy()
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

func TestStagger_ExpiredCreditsIgnored(t *testing.T) {
	// Credit opened 10 days ago with period 5 → already expired → daysUntil clamp to 1
	now := time.Date(2026, 3, 9, 0, 0, 0, 0, time.UTC)
	s := newStaggerAt(now)
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.ActiveCredits = []domain.FundingCredit{
			{Amount: 1000, Period: 5, OpenedAt: now.Add(-10 * 24 * time.Hour)},
		}
	})

	r := s.Apply(ctx)

	// Should still produce a result (expired credit goes to bucket[1])
	if len(r.Offers) != 1 {
		t.Fatalf("offers: got %d, want 1", len(r.Offers))
	}
	// Period range [2,30], bucket[1] is outside range, so all days in range are empty
	// Middle of [2..30] = 29 days, index 14 → day 16
	if r.Offers[0].Period != 16 {
		t.Errorf("period: got %d, want 16", r.Offers[0].Period)
	}
}

func TestStagger_PeriodWithinBounds(t *testing.T) {
	now := time.Date(2026, 3, 9, 0, 0, 0, 0, time.UTC)
	s := newStaggerAt(now)
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Config.Period.Min = 5
		c.Config.Period.Max = 10
		c.ActiveCredits = []domain.FundingCredit{
			{Amount: 5000, Period: 7, OpenedAt: now},
		}
	})

	r := s.Apply(ctx)

	p := r.Offers[0].Period
	if p < 5 || p > 10 {
		t.Errorf("period %d outside bounds [5, 10]", p)
	}
	// Day 7 is taken, so should pick another day
	if p == 7 {
		t.Errorf("period: got 7, should avoid the concentrated day")
	}
}
