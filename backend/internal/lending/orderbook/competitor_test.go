package orderbook

import (
	"math"
	"testing"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestCompetitor_ColdStart(t *testing.T) {
	cd := NewCompetitorDetector()
	entries := []domain.BookEntry{
		{Rate: 0.0001, Amount: 1000},
	}
	score := cd.Analyze(entries)
	if score != 0 {
		t.Errorf("expected 0 for cold start, got %f", score)
	}
}

func TestCompetitor_HighFollowRate(t *testing.T) {
	cd := NewCompetitorDetector()

	// Simulate 10 snapshots where new entries appear near best ask each time
	for i := 0; i < 10; i++ {
		bestAsk := 0.0001 + float64(i)*0.000001
		entries := []domain.BookEntry{
			{Rate: bestAsk, Amount: 1000},            // best ask
			{Rate: bestAsk + 0.0000001, Amount: 500}, // near best ask (new each time)
			{Rate: bestAsk + 0.001, Amount: 2000},    // far entry
		}
		cd.Analyze(entries)
	}

	// One more with a new near-best entry
	entries := []domain.BookEntry{
		{Rate: 0.00011, Amount: 1000},
		{Rate: 0.000110001, Amount: 500}, // new, near best ask
		{Rate: 0.001, Amount: 2000},
	}
	score := cd.Analyze(entries)
	// Should have elevated follow rate
	if score < 0.1 {
		t.Errorf("expected elevated score for high follow rate, got %f", score)
	}
}

func TestCompetitor_LowFollowRate(t *testing.T) {
	cd := NewCompetitorDetector()

	// Same entries every snapshot → no new entries near best ask
	entries := []domain.BookEntry{
		{Rate: 0.0001, Amount: 1000},
		{Rate: 0.0005, Amount: 2000},
	}
	for i := 0; i < 10; i++ {
		cd.Analyze(entries)
	}
	score := cd.Analyze(entries)
	// No new entries → follow rate = 0
	// Round number ratio: 0.0001 → 1 bps (round), 0.0005 → 5 bps (round) → ratio=1.0
	// score = 0.6×0 + 0.4×1.0 = 0.4
	if score > 0.5 {
		t.Errorf("expected low follow component, got %f", score)
	}
}

func TestCompetitor_RoundNumberHigh(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.0001, Amount: 1000}, // 1 bps → round
		{Rate: 0.0002, Amount: 1000}, // 2 bps → round
		{Rate: 0.0003, Amount: 1000}, // 3 bps → round
	}
	ratio := computeRoundNumberRatio(entries)
	if math.Abs(ratio-1.0) > 0.01 {
		t.Errorf("expected 1.0 for all round numbers, got %f", ratio)
	}
}

func TestCompetitor_RoundNumberLow(t *testing.T) {
	entries := []domain.BookEntry{
		{Rate: 0.000123, Amount: 1000}, // 1.23 bps → not round
		{Rate: 0.000156, Amount: 1000}, // 1.56 bps → not round
		{Rate: 0.000189, Amount: 1000}, // 1.89 bps → not round
		{Rate: 0.000200, Amount: 1000}, // 2.00 bps → round
		{Rate: 0.000234, Amount: 1000}, // 2.34 bps → not round
	}
	ratio := computeRoundNumberRatio(entries)
	// 1/5 = 0.2
	if math.Abs(ratio-0.2) > 0.01 {
		t.Errorf("expected 0.2, got %f", ratio)
	}
}

func TestCompetitor_CompositeScore(t *testing.T) {
	// Direct test: followRate=0.8, roundRatio=0.6
	// score = 0.6×0.8 + 0.4×0.6 = 0.48 + 0.24 = 0.72
	score := followWeight*0.8 + roundWeight*0.6
	if math.Abs(score-0.72) > 0.01 {
		t.Errorf("expected 0.72, got %f", score)
	}
}

func TestCompetitor_HistoryCap(t *testing.T) {
	cd := NewCompetitorDetector()
	entries := []domain.BookEntry{
		{Rate: 0.0001, Amount: 1000},
	}
	for i := 0; i < 20; i++ {
		cd.Analyze(entries)
	}
	if len(cd.history) > defaultSnapshotHistorySize {
		t.Errorf("history exceeded cap: %d > %d", len(cd.history), defaultSnapshotHistorySize)
	}
}
