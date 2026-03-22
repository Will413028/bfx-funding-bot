package orderbook

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestDetectGaps_Found(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.0001, Amount: 100},
		{Rate: 0.0002, Amount: 200},
		{Rate: 0.0005, Amount: 300}, // gap between 0.0002 and 0.0005
		{Rate: 0.0006, Amount: 100},
	}

	gaps := DetectGaps(entries, 0.00005, 0.0002)
	if len(gaps) != 1 {
		t.Fatalf("expected 1 gap, got %d", len(gaps))
	}
	if gaps[0].Low != 0.0002 || gaps[0].High != 0.0005 {
		t.Errorf("expected gap [0.0002, 0.0005], got [%f, %f]", gaps[0].Low, gaps[0].High)
	}
}

func TestDetectGaps_NoGap(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.0001, Amount: 100},
		{Rate: 0.00011, Amount: 200},
		{Rate: 0.00012, Amount: 300},
	}

	gaps := DetectGaps(entries, 0.00005, 0.0002)
	if len(gaps) != 0 {
		t.Errorf("expected 0 gaps, got %d", len(gaps))
	}
}

func TestDetectGaps_BidSideIgnored(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.0001, Amount: 100},  // ask
		{Rate: 0.0003, Amount: -200}, // bid (negative amount) — should be ignored
		{Rate: 0.0005, Amount: 300},  // ask — gap with 0.0001
	}

	gaps := DetectGaps(entries, 0.00005, 0.0002)
	if len(gaps) != 1 {
		t.Fatalf("expected 1 gap (bid ignored), got %d", len(gaps))
	}
}

func TestFindGapForRate_Inside(t *testing.T) {
	gaps := []domain.RateGap{{Low: 0.0002, High: 0.0005, Width: 0.0003}}
	g := FindGapForRate(gaps, 0.00035)
	if g == nil {
		t.Fatal("expected to find gap")
	}
	if g.Low != 0.0002 {
		t.Errorf("expected Low 0.0002, got %f", g.Low)
	}
}

func TestFindGapForRate_Outside(t *testing.T) {
	gaps := []domain.RateGap{{Low: 0.0002, High: 0.0005, Width: 0.0003}}
	g := FindGapForRate(gaps, 0.0001)
	if g != nil {
		t.Error("expected nil for rate outside gap")
	}
}
