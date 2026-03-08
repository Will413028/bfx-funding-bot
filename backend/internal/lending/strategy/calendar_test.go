package strategy

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestCalendar_MonthEnd_EventDay(t *testing.T) {
	// March 31 = last day of month → +5%
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 31, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	if r.Reason != "calendar:month_end" {
		t.Fatalf("reason: got %s, want calendar:month_end", r.Reason)
	}
	expected := 0.00025 * 1.05
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestCalendar_MonthEnd_2DaysBefore(t *testing.T) {
	// March 29 = 2 days before March 31 → +3%
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 29, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	expected := 0.00025 * 1.03
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestCalendar_MonthEnd_3DaysBefore(t *testing.T) {
	// April 27 = 3 days before April 30 → +2% (no quarterly overlap in April)
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 4, 27, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	expected := 0.00025 * 1.02
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestCalendar_MonthEnd_DayAfter(t *testing.T) {
	// April 1 = day after month end → +2%
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 4, 1, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	expected := 0.00025 * 1.02
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestCalendar_QuarterlyExpiry(t *testing.T) {
	// Last Friday of June 2026 = June 26 → quarterly event day → +10%
	lastFri := lastFridayOf(2026, time.June)
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 6, lastFri, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	if r.Reason != "calendar:quarterly" {
		t.Fatalf("reason: got %s, want calendar:quarterly", r.Reason)
	}
	expected := 0.00025 * 1.10
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestCalendar_QuarterlyExpiry_1DayBefore(t *testing.T) {
	lastFri := lastFridayOf(2026, time.June)
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 6, lastFri-1, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	expected := 0.00025 * 1.07
	if !approxEqual(r.Offers[0].Rate, expected, 1e-8) {
		t.Errorf("rate: got %f, want %f", r.Offers[0].Rate, expected)
	}
}

func TestCalendar_Overlap_QuarterlyWins(t *testing.T) {
	// Last Friday of March 2026: March 27 (Friday)
	// March 27 is also 4 days before month end (31st) → outside month-end range
	// But last Friday of March could be March 27, which is 4 days before → no month-end premium
	// Let's check: March 2026 has 31 days, March 27 is a Friday
	// daysToEnd = 31 - 27 = 4 → outside 1-3 range → no month-end premium
	// quarterly premium = +10% for event day → quarterly wins
	lastFri := lastFridayOf(2026, time.March)
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, lastFri, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	if r.Reason != "calendar:quarterly" {
		t.Fatalf("reason: got %s, want calendar:quarterly", r.Reason)
	}
}

func TestCalendar_PeriodShortening(t *testing.T) {
	// 2 days before month end → period should be limited to 2
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 29, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	if r.Offers[0].Period != 2 {
		t.Errorf("period: got %d, want 2", r.Offers[0].Period)
	}
}

func TestCalendar_PeriodShorteningClampedToMin(t *testing.T) {
	// 1 day before month end, config.Period.Min = 2 → period = max(2, 1) = 2
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 30, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	if r.Offers[0].Period < 2 {
		t.Errorf("period: got %d, want >= 2 (config min)", r.Offers[0].Period)
	}
}

func TestCalendar_NoEvent(t *testing.T) {
	// March 15 = mid-month, no quarterly → no adjustment
	s := NewCalendarStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.Timestamp = time.Date(2026, 3, 15, 12, 0, 0, 0, time.UTC)
	})

	r := s.Apply(ctx)

	if r.Reason != "calendar:none" {
		t.Fatalf("reason: got %s, want calendar:none", r.Reason)
	}
	if r.Offers[0].Rate != 0.00025 {
		t.Errorf("rate: got %f, want 0.00025 (unchanged)", r.Offers[0].Rate)
	}
}

func TestCalendar_FlashFreeze(t *testing.T) {
	s := NewCalendarStrategy()
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

func TestCalendar_InsufficientBalance(t *testing.T) {
	s := NewCalendarStrategy()
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
