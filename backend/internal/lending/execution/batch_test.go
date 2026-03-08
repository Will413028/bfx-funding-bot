package execution

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func batchConfig() *domain.StrategyConfig {
	return &domain.StrategyConfig{
		Currency: "fUSD",
		Amount:   domain.AmountConfig{Min: 50, Max: 10000},
		Rate:     domain.RateConfig{Min: 0.0002, Max: 0.005},
		Period:   domain.PeriodConfig{Min: 2, Max: 30},
	}
}

func makeCredit(id int64, amount, rate float64, period int, openedAt time.Time) domain.FundingCredit {
	return domain.FundingCredit{
		ID:        id,
		Currency:  "fUSD",
		Amount:    amount,
		Rate:      rate,
		Period:    period,
		Status:    "ACTIVE",
		AutoRenew: true,
		OpenedAt:  openedAt,
	}
}

func TestBatch_Empty(t *testing.T) {
	result := BatchCredits(nil, batchConfig(), time.Now())
	if len(result) != 0 {
		t.Errorf("expected empty, got %d batches", len(result))
	}
}

func TestBatch_SingleCredit(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	// Opens 8 days ago, period=10 → expires in 2 days
	c := makeCredit(101, 5000, 0.0003, 10, now.AddDate(0, 0, -8))

	batches := BatchCredits([]domain.FundingCredit{c}, batchConfig(), now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].Amount != 5000 {
		t.Errorf("amount: got %f, want 5000", batches[0].Amount)
	}
	if batches[0].Rate != 0.0003 {
		t.Errorf("rate: got %f, want 0.0003", batches[0].Rate)
	}
	if batches[0].Period != 10 {
		t.Errorf("period: got %d, want 10", batches[0].Period)
	}
	if len(batches[0].CreditIDs) != 1 || batches[0].CreditIDs[0] != 101 {
		t.Errorf("creditIDs: got %v, want [101]", batches[0].CreditIDs)
	}
}

func TestBatch_GroupByExpiryDay(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	// Day 1: opens 8 days ago, period=10 → expires Mar 10
	c1 := makeCredit(101, 1000, 0.0003, 10, now.AddDate(0, 0, -8))
	c2 := makeCredit(102, 2000, 0.0003, 10, now.AddDate(0, 0, -8))
	c3 := makeCredit(103, 3000, 0.0003, 10, now.AddDate(0, 0, -8))
	// Day 2: opens 3 days ago, period=10 → expires Mar 15
	c4 := makeCredit(104, 1500, 0.0004, 10, now.AddDate(0, 0, -3))
	c5 := makeCredit(105, 2500, 0.0004, 10, now.AddDate(0, 0, -3))

	batches := BatchCredits([]domain.FundingCredit{c1, c2, c3, c4, c5}, batchConfig(), now)
	if len(batches) != 2 {
		t.Fatalf("expected 2 batches, got %d", len(batches))
	}
	// First batch: day 1 (earlier)
	if batches[0].Amount != 6000 {
		t.Errorf("batch 0 amount: got %f, want 6000", batches[0].Amount)
	}
	if len(batches[0].CreditIDs) != 3 {
		t.Errorf("batch 0 creditIDs: got %d, want 3", len(batches[0].CreditIDs))
	}
	// Second batch: day 2
	if batches[1].Amount != 4000 {
		t.Errorf("batch 1 amount: got %f, want 4000", batches[1].Amount)
	}
	if len(batches[1].CreditIDs) != 2 {
		t.Errorf("batch 1 creditIDs: got %d, want 2", len(batches[1].CreditIDs))
	}
}

func TestBatch_AllSameDay(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	base := now.AddDate(0, 0, -8)
	credits := []domain.FundingCredit{
		makeCredit(101, 1000, 0.0003, 10, base),
		makeCredit(102, 2000, 0.0003, 10, base),
		makeCredit(103, 3000, 0.0003, 10, base),
		makeCredit(104, 1500, 0.0003, 10, base),
		makeCredit(105, 2500, 0.0003, 10, base.Add(6 * time.Hour)), // same day, different hour
	}

	batches := BatchCredits(credits, batchConfig(), now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].Amount != 10000 {
		t.Errorf("amount: got %f, want 10000", batches[0].Amount)
	}
}

func TestBatch_WeightedAverageRate(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	base := now.AddDate(0, 0, -8)
	// A: 1000 × 0.0002 = 0.2, B: 3000 × 0.0004 = 1.2 → total = 1.4/4000 = 0.00035
	credits := []domain.FundingCredit{
		makeCredit(101, 1000, 0.0002, 10, base),
		makeCredit(102, 3000, 0.0004, 10, base),
	}

	batches := BatchCredits(credits, batchConfig(), now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	expected := 0.00035
	if math.Abs(batches[0].Rate-expected) > 1e-10 {
		t.Errorf("rate: got %f, want %f", batches[0].Rate, expected)
	}
}

func TestBatch_MaxPeriod(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	// Different periods but same expiry day trick: use same openedAt, different periods
	// Actually periods affect expiry day calculation, so we need same expiry day
	// Use: c1 period=5, opened 5 days ago → expires today
	//      c2 period=15, opened 15 days ago → expires today
	c1 := makeCredit(101, 1000, 0.0003, 5, now.AddDate(0, 0, -5))
	c2 := makeCredit(102, 2000, 0.0003, 15, now.AddDate(0, 0, -15))

	batches := BatchCredits([]domain.FundingCredit{c1, c2}, batchConfig(), now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].Period != 15 {
		t.Errorf("period: got %d, want 15", batches[0].Period)
	}
}

func TestBatch_PeriodExceedsConfigMax(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	c1 := makeCredit(101, 1000, 0.0003, 5, now.AddDate(0, 0, -5))
	c2 := makeCredit(102, 2000, 0.0003, 15, now.AddDate(0, 0, -15))

	config := batchConfig()
	config.Period.Max = 12

	batches := BatchCredits([]domain.FundingCredit{c1, c2}, config, now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].Period != 12 {
		t.Errorf("period: got %d, want 12 (capped by config max)", batches[0].Period)
	}
}

func TestBatch_RateBelowConfigMin(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	base := now.AddDate(0, 0, -8)
	credits := []domain.FundingCredit{
		makeCredit(101, 1000, 0.0001, 10, base),
		makeCredit(102, 2000, 0.0001, 10, base),
	}

	config := batchConfig()
	config.Rate.Min = 0.0003

	batches := BatchCredits(credits, config, now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].Rate != 0.0003 {
		t.Errorf("rate: got %f, want 0.0003 (config min)", batches[0].Rate)
	}
}

func TestBatch_PeriodBelowConfigMin(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	c := makeCredit(101, 5000, 0.0003, 2, now.AddDate(0, 0, -2))

	config := batchConfig()
	config.Period.Min = 3

	batches := BatchCredits([]domain.FundingCredit{c}, config, now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].Period != 3 {
		t.Errorf("period: got %d, want 3 (config min)", batches[0].Period)
	}
}

func TestBatch_SplitOversized(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	base := now.AddDate(0, 0, -8)
	// Total = 25000, max = 10000 → 3 batches: 10000, 10000, 5000
	credits := []domain.FundingCredit{
		makeCredit(101, 8000, 0.0003, 10, base),
		makeCredit(102, 7000, 0.0003, 10, base),
		makeCredit(103, 6000, 0.0003, 10, base),
		makeCredit(104, 4000, 0.0003, 10, base),
	}

	batches := BatchCredits(credits, batchConfig(), now)
	if len(batches) != 3 {
		t.Fatalf("expected 3 batches (split), got %d", len(batches))
	}
	if batches[0].Amount != 10000 {
		t.Errorf("batch 0 amount: got %f, want 10000", batches[0].Amount)
	}
	if batches[1].Amount != 10000 {
		t.Errorf("batch 1 amount: got %f, want 10000", batches[1].Amount)
	}
	if batches[2].Amount != 5000 {
		t.Errorf("batch 2 amount: got %f, want 5000", batches[2].Amount)
	}
	// All sub-batches share same credit IDs
	if len(batches[0].CreditIDs) != 4 {
		t.Errorf("batch 0 creditIDs: got %d, want 4", len(batches[0].CreditIDs))
	}
	if len(batches[2].CreditIDs) != 4 {
		t.Errorf("batch 2 creditIDs: got %d, want 4", len(batches[2].CreditIDs))
	}
}

func TestBatch_AmountWithinMax(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	base := now.AddDate(0, 0, -8)
	credits := []domain.FundingCredit{
		makeCredit(101, 3000, 0.0003, 10, base),
		makeCredit(102, 5000, 0.0003, 10, base),
	}

	batches := BatchCredits(credits, batchConfig(), now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	if batches[0].Amount != 8000 {
		t.Errorf("amount: got %f, want 8000", batches[0].Amount)
	}
}

func TestBatch_CreditIDsPreserved(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	base := now.AddDate(0, 0, -8)
	credits := []domain.FundingCredit{
		makeCredit(101, 1000, 0.0003, 10, base),
		makeCredit(102, 2000, 0.0003, 10, base),
		makeCredit(103, 3000, 0.0003, 10, base),
	}

	batches := BatchCredits(credits, batchConfig(), now)
	if len(batches) != 1 {
		t.Fatalf("expected 1 batch, got %d", len(batches))
	}
	ids := batches[0].CreditIDs
	if len(ids) != 3 {
		t.Fatalf("creditIDs length: got %d, want 3", len(ids))
	}
	expected := map[int64]bool{101: true, 102: true, 103: true}
	for _, id := range ids {
		if !expected[id] {
			t.Errorf("unexpected credit ID: %d", id)
		}
	}
}

func TestBatch_SortedChronologically(t *testing.T) {
	now := time.Date(2026, 3, 8, 12, 0, 0, 0, time.UTC)
	// Later expiry first in input, should be sorted
	cLater := makeCredit(102, 2000, 0.0003, 10, now.AddDate(0, 0, -3))  // expires Mar 15
	cEarlier := makeCredit(101, 1000, 0.0003, 10, now.AddDate(0, 0, -8)) // expires Mar 10

	batches := BatchCredits([]domain.FundingCredit{cLater, cEarlier}, batchConfig(), now)
	if len(batches) != 2 {
		t.Fatalf("expected 2 batches, got %d", len(batches))
	}
	if !batches[0].ExpiryDate.Before(batches[1].ExpiryDate) {
		t.Errorf("batches not sorted chronologically: %v >= %v", batches[0].ExpiryDate, batches[1].ExpiryDate)
	}
}
