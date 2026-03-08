package orderbook

import (
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestDetectWalls_Empty(t *testing.T) {
	walls := DetectWalls(nil, 0.0001, nil)
	if len(walls) != 0 {
		t.Errorf("expected no walls, got %d", len(walls))
	}
}

func TestDetectWalls_SingleWall(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.001, Period: 2, Count: 1, Amount: 5000},  // 10% → wall
		{Rate: 0.0011, Period: 2, Count: 1, Amount: 1000}, // 2% → not wall
		{Rate: 0.0012, Period: 2, Count: 1, Amount: 44000},
	}
	walls := DetectWalls(entries, 0.0001, nil)
	singleCount := 0
	for _, w := range walls {
		if w.Type == domain.WallSingle {
			singleCount++
		}
	}
	// 5000/50000=10% > 5% → wall, 1000/50000=2% → no, 44000/50000=88% → wall
	if singleCount != 2 {
		t.Errorf("expected 2 single walls, got %d", singleCount)
	}
}

func TestDetectWalls_BelowThreshold(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.001, Period: 2, Count: 1, Amount: 1000},
		{Rate: 0.0011, Period: 2, Count: 1, Amount: 1000},
		{Rate: 0.0012, Period: 2, Count: 1, Amount: 1000},
	}
	// Each is 33%, cluster is 100%. Use spread=0 to disable distributed detection.
	// With threshold 0.50, no single entry reaches 50%.
	walls := DetectWalls(entries, 0, &WallOptions{Threshold: 0.50})
	if len(walls) != 0 {
		t.Errorf("expected 0 walls with 50%% threshold, got %d", len(walls))
	}
}

func TestDetectWalls_DistributedWall(t *testing.T) {
	// spread = 0.00005
	// cluster factor default = 2.0, so maxGap = 0.0001
	entries := []domain.BookEntry{
		// 3 adjacent offers, each ~4% individually but clustered = 12%
		{Rate: 0.00100, Period: 2, Count: 1, Amount: 2000},
		{Rate: 0.00101, Period: 2, Count: 1, Amount: 2000},
		{Rate: 0.00102, Period: 2, Count: 1, Amount: 2000},
		// Far away offer (single wall)
		{Rate: 0.00200, Period: 2, Count: 1, Amount: 44000},
	}
	// total offer = 50000
	// Each of the 3 close entries: 4% → none is single wall (< 5%)
	// 44000/50000 = 88% → single wall
	// Cluster of 3: 6000/50000 = 12% > 5% → distributed wall
	walls := DetectWalls(entries, 0.00005, nil)

	var single, distributed int
	for _, w := range walls {
		if w.Type == domain.WallSingle {
			single++
		} else if w.Type == domain.WallDistributed {
			distributed++
		}
	}
	if single != 1 {
		t.Errorf("expected 1 single wall, got %d", single)
	}
	if distributed != 1 {
		t.Errorf("expected 1 distributed wall, got %d", distributed)
	}
}

func TestDetectWalls_NonAdjacentNotClustered(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.00100, Period: 2, Count: 1, Amount: 3000},
		{Rate: 0.00200, Period: 2, Count: 1, Amount: 3000}, // gap = 0.001 > maxGap
		{Rate: 0.00300, Period: 2, Count: 1, Amount: 44000},
	}
	// spread = 0.00005, maxGap = 0.0001
	// Entries too far apart → no distributed wall
	walls := DetectWalls(entries, 0.00005, nil)
	for _, w := range walls {
		if w.Type == domain.WallDistributed {
			t.Error("should not detect distributed wall for non-adjacent entries")
		}
	}
}

func TestDetectWalls_BidSide(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.0009, Period: 2, Count: 1, Amount: -8000},  // 80% → wall
		{Rate: 0.0008, Period: 2, Count: 1, Amount: -2000},  // 20% → wall
	}
	walls := DetectWalls(entries, 0.0001, nil)
	for _, w := range walls {
		if w.Side != "bid" {
			t.Errorf("expected bid side, got %s", w.Side)
		}
	}
	if len(walls) < 1 {
		t.Error("expected at least 1 bid wall")
	}
}

func TestDetectWalls_DistributedWallOutput(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.00100, Period: 2, Count: 2, Amount: 2000},
		{Rate: 0.00101, Period: 2, Count: 3, Amount: 2000},
		{Rate: 0.00102, Period: 2, Count: 1, Amount: 2000},
		{Rate: 0.00500, Period: 2, Count: 1, Amount: 44000},
	}
	// total = 50000. Each close entry = 4% (< 5% single threshold).
	// 44000/50000 = 88% → single wall. Cluster of 3: 6000/50000 = 12% → distributed.
	walls := DetectWalls(entries, 0.00005, nil)
	for _, w := range walls {
		if w.Type == domain.WallDistributed {
			expectedRate := (0.00100 + 0.00101 + 0.00102) / 3
			if w.Rate < expectedRate-1e-10 || w.Rate > expectedRate+1e-10 {
				t.Errorf("expected avg rate %f, got %f", expectedRate, w.Rate)
			}
			if w.Amount != 6000 {
				t.Errorf("expected amount 6000, got %f", w.Amount)
			}
			if w.EntryCount != 6 {
				t.Errorf("expected entry count 6, got %d", w.EntryCount)
			}
		}
	}
}
