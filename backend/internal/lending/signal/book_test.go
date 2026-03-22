package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestBookConsumption_InsufficientHistory(t *testing.T) {
	bc := NewBookConsumption()
	now := time.Now()
	data := &domain.RawMarketData{
		Book: []domain.BookEntry{
			{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},
		},
		Timestamp: now,
	}
	sv := bc.Compute(data)
	if sv.Value != 0 || sv.Confidence != 0 {
		t.Errorf("expected zero signal for single snapshot, got value=%f conf=%f", sv.Value, sv.Confidence)
	}
}

func TestBookConsumption_StableDepth(t *testing.T) {
	bc := NewBookConsumption()
	now := time.Now()
	book := []domain.BookEntry{
		{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},  // offer
		{Rate: 0.0008, Period: 2, Count: 1, Amount: -500}, // bid
	}
	for i := 0; i < 3; i++ {
		data := &domain.RawMarketData{Book: book, Timestamp: now.Add(time.Duration(i) * time.Second)}
		bc.Compute(data)
	}
	sv := bc.Compute(&domain.RawMarketData{Book: book, Timestamp: now.Add(4 * time.Second)})
	// Stable depth → changeRate ≈ 0, signal ≈ 0
	if sv.Value < -0.01 || sv.Value > 0.01 {
		t.Errorf("expected near-zero signal for stable depth, got %f", sv.Value)
	}
}

func TestBookConsumption_ShrinkingDepth(t *testing.T) {
	bc := NewBookConsumption()
	now := time.Now()

	// First snapshot: large depth
	bc.Compute(&domain.RawMarketData{
		Book: []domain.BookEntry{
			{Rate: 0.001, Period: 2, Count: 1, Amount: 10000},
			{Rate: 0.0008, Period: 2, Count: 1, Amount: -5000},
		},
		Timestamp: now,
	})
	// Second snapshot: smaller depth → consumption
	sv := bc.Compute(&domain.RawMarketData{
		Book: []domain.BookEntry{
			{Rate: 0.001, Period: 2, Count: 1, Amount: 5000},
			{Rate: 0.0008, Period: 2, Count: 1, Amount: -2500},
		},
		Timestamp: now.Add(3 * time.Second),
	})
	// Depth shrunk 50% → positive signal (demand pressure)
	if sv.Value <= 0 {
		t.Errorf("expected positive signal for shrinking depth, got %f", sv.Value)
	}
}

func TestBookConsumption_GrowingDepth(t *testing.T) {
	bc := NewBookConsumption()
	now := time.Now()

	bc.Compute(&domain.RawMarketData{
		Book: []domain.BookEntry{
			{Rate: 0.001, Period: 2, Count: 1, Amount: 5000},
			{Rate: 0.0008, Period: 2, Count: 1, Amount: -2500},
		},
		Timestamp: now,
	})
	sv := bc.Compute(&domain.RawMarketData{
		Book: []domain.BookEntry{
			{Rate: 0.001, Period: 2, Count: 1, Amount: 10000},
			{Rate: 0.0008, Period: 2, Count: 1, Amount: -5000},
		},
		Timestamp: now.Add(3 * time.Second),
	})
	// Depth grew → negative signal (supply pressure)
	if sv.Value >= 0 {
		t.Errorf("expected negative signal for growing depth, got %f", sv.Value)
	}
}

func TestBookConsumption_HistoryCap(t *testing.T) {
	bc := NewBookConsumption()
	now := time.Now()
	book := []domain.BookEntry{
		{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},
	}
	for i := 0; i < 20; i++ {
		bc.Compute(&domain.RawMarketData{Book: book, Timestamp: now.Add(time.Duration(i) * time.Second)})
	}
	if len(bc.history) > bookHistorySize {
		t.Errorf("history exceeded cap: %d > %d", len(bc.history), bookHistorySize)
	}
}
