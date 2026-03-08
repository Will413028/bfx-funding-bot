package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func makeRawData(t time.Time) *domain.RawMarketData {
	return &domain.RawMarketData{Symbol: "fUSD", Timestamp: t}
}

func utcTime(year, month, day, hour, min int) time.Time {
	return time.Date(year, time.Month(month), day, hour, min, 0, 0, time.UTC)
}

func TestIntraday_Name(t *testing.T) {
	s := NewIntraday()
	if s.Name() != "intraday" {
		t.Errorf("expected 'intraday', got %q", s.Name())
	}
}

func TestIntraday_AsiaSession(t *testing.T) {
	s := NewIntraday()
	// Mid-session: UTC 01:30, mid-month day 15
	sv := s.Compute(makeRawData(utcTime(2026, 3, 15, 1, 30)))
	if sv.Value < 0.3 || sv.Value > 0.5 {
		t.Errorf("Asia session: expected [0.3, 0.5], got %f", sv.Value)
	}
	if sv.Type != domain.SignalIntraday {
		t.Errorf("expected SignalIntraday, got %s", sv.Type)
	}
	if sv.Confidence != 1.0 {
		t.Errorf("expected confidence 1.0, got %f", sv.Confidence)
	}
}

func TestIntraday_EuropeSession(t *testing.T) {
	s := NewIntraday()
	// Mid-session: UTC 08:00, mid-month
	sv := s.Compute(makeRawData(utcTime(2026, 3, 15, 8, 0)))
	if sv.Value < 0.3 || sv.Value > 0.5 {
		t.Errorf("Europe session: expected [0.3, 0.5], got %f", sv.Value)
	}
}

func TestIntraday_USSession(t *testing.T) {
	s := NewIntraday()
	// Mid-session: UTC 14:00, mid-month
	sv := s.Compute(makeRawData(utcTime(2026, 3, 15, 14, 0)))
	if sv.Value < 0.3 || sv.Value > 0.5 {
		t.Errorf("US session: expected [0.3, 0.5], got %f", sv.Value)
	}
}

func TestIntraday_OutsideSessions(t *testing.T) {
	s := NewIntraday()
	// UTC 05:00 — between Asia end+transition and Europe start-transition
	sv := s.Compute(makeRawData(utcTime(2026, 3, 15, 5, 0)))
	if sv.Value != -0.3 {
		t.Errorf("outside sessions: expected -0.3, got %f", sv.Value)
	}
}

func TestIntraday_EnteringTransition(t *testing.T) {
	s := NewIntraday()
	// 15 min before Europe open (06:45) → halfway through transition
	sv := s.Compute(makeRawData(utcTime(2026, 3, 15, 6, 45)))
	// Should be between -0.3 and 0.40 (Europe peak)
	if sv.Value <= -0.3 || sv.Value >= 0.40 {
		t.Errorf("entering transition: expected (-0.3, 0.40), got %f", sv.Value)
	}
}

func TestIntraday_LeavingTransition(t *testing.T) {
	s := NewIntraday()
	// 15 min after Europe close (09:15) → halfway through exit transition
	sv := s.Compute(makeRawData(utcTime(2026, 3, 15, 9, 15)))
	// Should be between -0.3 and 0.40
	if sv.Value <= -0.3 || sv.Value >= 0.40 {
		t.Errorf("leaving transition: expected (-0.3, 0.40), got %f", sv.Value)
	}
}

func TestIntraday_MonthEnd(t *testing.T) {
	s := NewIntraday()
	// Day 30, inside US session (peak 0.45 * 1.1 = 0.495, clamped to 0.495)
	sv := s.Compute(makeRawData(utcTime(2026, 3, 30, 14, 0)))
	expected := 0.45 * 1.1 // 0.495
	if sv.Value < expected-0.01 || sv.Value > expected+0.01 {
		t.Errorf("month-end US session: expected ~%f, got %f", expected, sv.Value)
	}
}

func TestIntraday_MonthStart(t *testing.T) {
	s := NewIntraday()
	// Day 2, inside US session (peak 0.45 * 1.05 = 0.4725)
	sv := s.Compute(makeRawData(utcTime(2026, 4, 2, 14, 0)))
	expected := 0.45 * 1.05
	if sv.Value < expected-0.01 || sv.Value > expected+0.01 {
		t.Errorf("month-start US session: expected ~%f, got %f", expected, sv.Value)
	}
}

func TestIntraday_ClampUpperBound(t *testing.T) {
	s := NewIntraday()
	// Day 31, Asia session peak 0.45 * 1.1 = 0.495 → should not exceed 0.5
	sv := s.Compute(makeRawData(utcTime(2026, 3, 31, 1, 30)))
	if sv.Value > 0.5 {
		t.Errorf("clamp: expected <= 0.5, got %f", sv.Value)
	}
}

func TestIntraday_ClampLowerBound(t *testing.T) {
	s := NewIntraday()
	// Outside sessions, month-end: -0.3 * 1.1 = -0.33, should not go below -0.5
	sv := s.Compute(makeRawData(utcTime(2026, 3, 31, 5, 0)))
	if sv.Value < -0.5 {
		t.Errorf("clamp: expected >= -0.5, got %f", sv.Value)
	}
}

func TestIntraday_Stateless(t *testing.T) {
	s := NewIntraday()
	ts := utcTime(2026, 3, 15, 8, 0)

	sv1 := s.Compute(makeRawData(ts))
	// Call with different time in between
	s.Compute(makeRawData(utcTime(2026, 3, 15, 20, 0)))
	sv2 := s.Compute(makeRawData(ts))

	if sv1.Value != sv2.Value {
		t.Errorf("stateless: same input should produce same output, got %f vs %f", sv1.Value, sv2.Value)
	}
}
