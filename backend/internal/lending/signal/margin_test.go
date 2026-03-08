package signal

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestMarginUsage_InsufficientHistory(t *testing.T) {
	mu := NewMarginUsage()
	now := time.Now()
	sv := mu.Compute(&domain.RawMarketData{
		Book: []domain.BookEntry{
			{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},
			{Rate: 0.0008, Period: 2, Count: 1, Amount: -500},
		},
		Timestamp: now,
	})
	if sv.Value != 0 || sv.Confidence != 0 {
		t.Errorf("expected zero for single snapshot, got value=%f conf=%f", sv.Value, sv.Confidence)
	}
}

func TestMarginUsage_BalancedBook(t *testing.T) {
	mu := NewMarginUsage()
	now := time.Now()
	book := []domain.BookEntry{
		{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},   // offer
		{Rate: 0.0008, Period: 2, Count: 1, Amount: -1000},  // bid (equal)
	}
	for i := 0; i < 5; i++ {
		mu.Compute(&domain.RawMarketData{Book: book, Timestamp: now.Add(time.Duration(i) * time.Second)})
	}
	sv := mu.Compute(&domain.RawMarketData{Book: book, Timestamp: now.Add(6 * time.Second)})
	// Stable balanced book → signal ≈ 0
	if math.Abs(sv.Value) > 0.1 {
		t.Errorf("expected near-zero for balanced book, got %f", sv.Value)
	}
}

func TestMarginUsage_IncreasingBidDominance(t *testing.T) {
	mu := NewMarginUsage()
	now := time.Now()

	// Start balanced
	for i := 0; i < 5; i++ {
		mu.Compute(&domain.RawMarketData{
			Book: []domain.BookEntry{
				{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},
				{Rate: 0.0008, Period: 2, Count: 1, Amount: -1000},
			},
			Timestamp: now.Add(time.Duration(i) * time.Second),
		})
	}

	// Shift to bid-heavy
	sv := mu.Compute(&domain.RawMarketData{
		Book: []domain.BookEntry{
			{Rate: 0.001, Period: 2, Count: 1, Amount: 200},    // offer (small)
			{Rate: 0.0008, Period: 2, Count: 1, Amount: -5000}, // bid (large)
		},
		Timestamp: now.Add(6 * time.Second),
	})
	// More bid pressure than historical → positive signal
	if sv.Value <= 0 {
		t.Errorf("expected positive signal for bid dominance, got %f", sv.Value)
	}
}

func TestMarginUsage_HistoryCap(t *testing.T) {
	mu := NewMarginUsage()
	now := time.Now()
	book := []domain.BookEntry{
		{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},
		{Rate: 0.0008, Period: 2, Count: 1, Amount: -500},
	}
	for i := 0; i < 30; i++ {
		mu.Compute(&domain.RawMarketData{Book: book, Timestamp: now.Add(time.Duration(i) * time.Second)})
	}
	if len(mu.history) > marginHistorySize {
		t.Errorf("history exceeded cap: %d > %d", len(mu.history), marginHistorySize)
	}
}
