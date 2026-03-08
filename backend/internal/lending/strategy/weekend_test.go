package strategy

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestWeekend_Weekday(t *testing.T) {
	// Wednesday 14:00 UTC → no premium
	s := NewWeekendStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 11, 14, 0, 0, 0, time.UTC) // Wednesday
	})

	r := s.Apply(ctx)

	if r.Reason != "weekend:weekday" {
		t.Fatalf("reason: got %s, want weekend:weekday", r.Reason)
	}
	if r.Offers[0].Rate != 0.00025 {
		t.Errorf("rate: got %f, want 0.00025 (unchanged)", r.Offers[0].Rate)
	}
}

func TestWeekend_Thursday(t *testing.T) {
	s := NewWeekendStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 12, 10, 0, 0, 0, time.UTC) // Thursday
	})

	r := s.Apply(ctx)

	if r.Reason != "weekend:weekday" {
		t.Fatalf("reason: got %s, want weekend:weekday", r.Reason)
	}
}

func TestWeekend_FridayMorning(t *testing.T) {
	// Friday 10:00 → before 18:00, no premium
	s := NewWeekendStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 13, 10, 0, 0, 0, time.UTC) // Friday
	})

	r := s.Apply(ctx)

	if r.Reason != "weekend:weekday" {
		t.Fatalf("reason: got %s, want weekend:weekday", r.Reason)
	}
	if r.Offers[0].Rate != 0.00025 {
		t.Errorf("rate: got %f, want 0.00025 (unchanged)", r.Offers[0].Rate)
	}
}

func TestWeekend_FridayEvening(t *testing.T) {
	// Friday 20:00 → +2%
	s := NewWeekendStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 13, 20, 0, 0, 0, time.UTC) // Friday
	})

	r := s.Apply(ctx)

	if r.Reason != "weekend:friday_eve" {
		t.Fatalf("reason: got %s, want weekend:friday_eve", r.Reason)
	}
	expected := 0.00025 * 1.02
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestWeekend_Saturday(t *testing.T) {
	// Saturday → +5%
	s := NewWeekendStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 14, 12, 0, 0, 0, time.UTC) // Saturday
	})

	r := s.Apply(ctx)

	if r.Reason != "weekend:saturday" {
		t.Fatalf("reason: got %s, want weekend:saturday", r.Reason)
	}
	expected := 0.00025 * 1.05
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestWeekend_Sunday(t *testing.T) {
	// Sunday → +3%
	s := NewWeekendStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 15, 15, 0, 0, 0, time.UTC) // Sunday
	})

	r := s.Apply(ctx)

	if r.Reason != "weekend:sunday" {
		t.Fatalf("reason: got %s, want weekend:sunday", r.Reason)
	}
	expected := 0.00025 * 1.03
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestWeekend_RateClampedToMax(t *testing.T) {
	// High FRR on Saturday: 0.0049 × 1.05 = 0.005145 → clamped to 0.005
	s := NewWeekendStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0049
		c.Config.Rate.Max = 0.005
		c.Snapshot.Timestamp = time.Date(2026, 3, 14, 12, 0, 0, 0, time.UTC) // Saturday
	})

	r := s.Apply(ctx)

	if r.Offers[0].Rate != 0.005 {
		t.Errorf("rate: got %f, want 0.005 (clamped)", r.Offers[0].Rate)
	}
}

func TestWeekend_FlashFreeze(t *testing.T) {
	s := NewWeekendStrategy()
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

func TestWeekend_InsufficientBalance(t *testing.T) {
	s := NewWeekendStrategy()
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
