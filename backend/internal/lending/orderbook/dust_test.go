package orderbook

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestFilterDust_Empty(t *testing.T) {
	result := FilterDust(nil, nil)
	if len(result) != 0 {
		t.Errorf("expected empty, got %d", len(result))
	}
}

func TestFilterDust_AdaptiveThreshold(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.001, Amount: 10},    // dust (median=500, threshold=25)
		{Rate: 0.0011, Amount: 15},   // dust
		{Rate: 0.0012, Amount: 500},  // kept (median)
		{Rate: 0.0013, Amount: 1000}, // kept
		{Rate: 0.0014, Amount: 5000}, // kept
	}
	result := FilterDust(entries, nil)
	if len(result) != 3 {
		t.Errorf("expected 3 entries after dust filter, got %d", len(result))
	}
}

func TestFilterDust_BothSidesIndependent(t *testing.T) {
	entries := []domain.BookEntry{
		// Offers: median=1000, threshold=50
		{Rate: 0.001, Amount: 10},     // dust
		{Rate: 0.0011, Amount: 1000},  // kept
		{Rate: 0.0012, Amount: 5000},  // kept
		// Bids: median=200, threshold=10
		{Rate: 0.0009, Amount: -5},    // dust
		{Rate: 0.0008, Amount: -200},  // kept
		{Rate: 0.0007, Amount: -1000}, // kept
	}
	result := FilterDust(entries, nil)
	// 2 offers + 2 bids = 4
	offerCount := 0
	bidCount := 0
	for _, e := range result {
		if e.Amount > 0 {
			offerCount++
		} else {
			bidCount++
		}
	}
	if offerCount != 2 {
		t.Errorf("expected 2 offers after filter, got %d", offerCount)
	}
	if bidCount != 2 {
		t.Errorf("expected 2 bids after filter, got %d", bidCount)
	}
}

func TestFilterDust_CustomFactor(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.001, Amount: 50},
		{Rate: 0.0011, Amount: 100},
		{Rate: 0.0012, Amount: 1000},
	}
	// median=100, factor=0.10, threshold=10 → only 50 is kept (>=10)
	// Actually all >= 10, so all kept
	result := FilterDust(entries, &DustOptions{Factor: 0.50})
	// median=100, factor=0.50, threshold=50 → 50 kept (==50), 100 kept, 1000 kept
	if len(result) != 3 {
		t.Errorf("expected 3 with factor=0.50, got %d", len(result))
	}

	// factor=1.5, threshold=150 → only 1000 kept
	result = FilterDust(entries, &DustOptions{Factor: 1.5})
	if len(result) != 1 {
		t.Errorf("expected 1 with factor=1.5, got %d", len(result))
	}
}

func TestFilterDust_PreservesOriginal(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.001, Amount: 1},
		{Rate: 0.0011, Amount: 1000},
	}
	original := make([]domain.BookEntry, len(entries))
	copy(original, entries)

	FilterDust(entries, nil)

	for i, e := range entries {
		if e != original[i] {
			t.Error("original entries were modified")
		}
	}
}
