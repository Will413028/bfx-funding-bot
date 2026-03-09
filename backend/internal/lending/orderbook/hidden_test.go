package orderbook

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestHiddenRatio_TradeVolumeExceedsDepth(t *testing.T) {
	h := NewHiddenRatioEstimator()
	now := time.Now()

	book := []domain.BookEntry{
		{Rate: 0.001, Amount: 60000}, // visible offer depth = 60000
	}
	trades := []domain.FundingTradeRecord{
		{Amount: 50000, MTS: now.Add(-1 * time.Minute)},
		{Amount: 50000, MTS: now.Add(-2 * time.Minute)},
	}
	// trade volume = 100000, visible = 60000
	// ratio = (100000 - 60000) / 100000 = 0.4
	ratio := h.Estimate(book, trades, now)
	if math.Abs(ratio-0.4) > 0.01 {
		t.Errorf("expected ~0.4, got %f", ratio)
	}
}

func TestHiddenRatio_VisibleExceedsTrades(t *testing.T) {
	h := NewHiddenRatioEstimator()
	now := time.Now()

	book := []domain.BookEntry{
		{Rate: 0.001, Amount: 60000},
	}
	trades := []domain.FundingTradeRecord{
		{Amount: 15000, MTS: now.Add(-1 * time.Minute)},
		{Amount: 15000, MTS: now.Add(-2 * time.Minute)},
	}
	// trade volume = 30000, visible = 60000 → clamped to 0
	ratio := h.Estimate(book, trades, now)
	if ratio != 0 {
		t.Errorf("expected 0 (clamped), got %f", ratio)
	}
}

func TestHiddenRatio_LowVolume(t *testing.T) {
	h := NewHiddenRatioEstimator()
	now := time.Now()

	book := []domain.BookEntry{
		{Rate: 0.001, Amount: 1000},
	}
	trades := []domain.FundingTradeRecord{
		{Amount: 5000, MTS: now.Add(-1 * time.Minute)},
	}
	// trade volume = 5000 < minVol (10000) → 0
	ratio := h.Estimate(book, trades, now)
	if ratio != 0 {
		t.Errorf("expected 0 for low volume, got %f", ratio)
	}
}

func TestHiddenRatio_CallerPrunesOldTrades(t *testing.T) {
	h := NewHiddenRatioEstimator()
	now := time.Now()

	book := []domain.BookEntry{
		{Rate: 0.001, Amount: 5000},
	}

	// Caller is responsible for pruning old trades before passing to Estimate.
	// Only recent trades are included in the buffer.
	recentTrades := []domain.FundingTradeRecord{
		{Amount: 20000, MTS: now.Add(-1 * time.Minute)},
	}
	ratio := h.Estimate(book, recentTrades, now)
	// volume = 20000, visible = 5000
	// ratio = (20000 - 5000) / 20000 = 0.75
	if math.Abs(ratio-0.75) > 0.01 {
		t.Errorf("expected ~0.75, got %f", ratio)
	}
}

func TestHiddenRatio_FullWindowBuffer(t *testing.T) {
	h := NewHiddenRatioEstimator()
	now := time.Now()

	book := []domain.BookEntry{
		{Rate: 0.001, Amount: 10000},
	}

	// Caller passes the complete windowed trade buffer each time.
	// Simulate three ticks where the buffer grows as new trades arrive.
	allTrades := []domain.FundingTradeRecord{
		{Amount: 10000, MTS: now.Add(-3 * time.Minute)},
		{Amount: 20000, MTS: now.Add(-2 * time.Minute)},
		{Amount: 15000, MTS: now.Add(-1 * time.Minute)},
	}
	ratio := h.Estimate(book, allTrades, now)
	// total volume = 45000, visible = 10000
	// ratio = (45000 - 10000) / 45000 ≈ 0.778
	if ratio < 0.7 || ratio > 0.8 {
		t.Errorf("expected ~0.78, got %f", ratio)
	}
}
