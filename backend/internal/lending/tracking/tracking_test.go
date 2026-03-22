package tracking

import (
	"math"
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// --- Alpha ---

func TestComputeAlpha_Positive(t *testing.T) {
	alpha := ComputeAlpha(0.0005, 0.0003)
	if math.Abs(alpha-0.0002) > 1e-10 {
		t.Errorf("expected 0.0002, got %f", alpha)
	}
}

func TestComputeAlpha_Negative(t *testing.T) {
	alpha := ComputeAlpha(0.0002, 0.0003)
	if math.Abs(alpha-(-0.0001)) > 1e-10 {
		t.Errorf("expected -0.0001, got %f", alpha)
	}
}

func TestNewPerformanceRecord(t *testing.T) {
	snap := &domain.MarketSnapshot{
		FRR:    0.0003,
		MDC:    domain.MDCResult{Score: 0.7},
		Regime: domain.RegimeContango,
	}
	decision := &domain.DecisionResult{
		StrategyTags: []string{"pricing", "weekend"},
		Reason:       "pricing",
	}
	now := time.Now()

	rec := NewPerformanceRecord("user-1", 0.0005, snap, decision, 1000, 7, "fUSD", now)

	if math.Abs(rec.Alpha-0.0002) > 1e-10 {
		t.Errorf("alpha: expected 0.0002, got %f", rec.Alpha)
	}
	if rec.Regime != domain.RegimeContango {
		t.Errorf("regime: expected contango, got %s", rec.Regime)
	}
	if len(rec.StrategyTags) != 2 {
		t.Errorf("tags: expected 2, got %d", len(rec.StrategyTags))
	}
}

// --- Summary ---

func TestSummarize_Basic(t *testing.T) {
	now := time.Now()
	records := []domain.PerformanceRecord{
		{Alpha: 0.0002, Regime: domain.RegimeContango, Timestamp: now},
		{Alpha: -0.0001, Regime: domain.RegimeContango, Timestamp: now},
		{Alpha: 0.0003, Regime: domain.RegimeNeutral, Timestamp: now},
	}

	summary := Summarize(records, now.Add(-1*time.Hour))

	if summary.Count != 3 {
		t.Errorf("count: expected 3, got %d", summary.Count)
	}
	expectedTotal := 0.0004
	if math.Abs(summary.TotalAlpha-expectedTotal) > 1e-10 {
		t.Errorf("total: expected %f, got %f", expectedTotal, summary.TotalAlpha)
	}
	if len(summary.ByRegime) != 2 {
		t.Errorf("regimes: expected 2, got %d", len(summary.ByRegime))
	}
}

func TestSummarize_FiltersOldRecords(t *testing.T) {
	now := time.Now()
	records := []domain.PerformanceRecord{
		{Alpha: 0.001, Timestamp: now.Add(-48 * time.Hour)}, // old
		{Alpha: 0.002, Timestamp: now},                      // recent
	}

	summary := Summarize(records, now.Add(-24*time.Hour))

	if summary.Count != 1 {
		t.Errorf("expected 1 (only recent), got %d", summary.Count)
	}
}

// --- Attribution ---

func TestAttributeByTag_GroupsByTag(t *testing.T) {
	records := []domain.PerformanceRecord{
		{Alpha: 0.0002, StrategyTags: []string{"pricing", "weekend"}, Regime: domain.RegimeContango},
		{Alpha: 0.0001, StrategyTags: []string{"pricing"}, Regime: domain.RegimeContango},
		{Alpha: -0.0001, StrategyTags: []string{"weekend"}, Regime: domain.RegimeContango},
	}

	tags := AttributeByTag(records, false)

	tagMap := make(map[string]domain.TagAlpha)
	for _, ta := range tags {
		tagMap[ta.Tag] = ta
	}

	if tagMap["pricing"].Count != 2 {
		t.Errorf("pricing count: expected 2, got %d", tagMap["pricing"].Count)
	}
	if tagMap["weekend"].Count != 2 {
		t.Errorf("weekend count: expected 2, got %d", tagMap["weekend"].Count)
	}
}

func TestAttributeByTag_SplitByRegime(t *testing.T) {
	records := []domain.PerformanceRecord{
		{Alpha: 0.0002, StrategyTags: []string{"pricing"}, Regime: domain.RegimeContango},
		{Alpha: -0.0001, StrategyTags: []string{"pricing"}, Regime: domain.RegimeBackwardation},
	}

	tags := AttributeByTag(records, true)

	if len(tags) != 2 {
		t.Errorf("expected 2 (one per regime), got %d", len(tags))
	}
}

// --- Feedback ---

func TestGenerateAdjustments_PositiveAlpha(t *testing.T) {
	tagAlphas := []domain.TagAlpha{
		{Tag: "weekend", MeanAlpha: 0.00015, Variance: 0.00001, Count: 10},
	}

	adjs := GenerateAdjustments(tagAlphas)

	if len(adjs) != 1 {
		t.Fatalf("expected 1 adjustment, got %d", len(adjs))
	}
	if adjs[0].Direction != "increase" {
		t.Errorf("expected increase, got %s", adjs[0].Direction)
	}
	if adjs[0].Percentage > maxAdjustmentPct {
		t.Errorf("adjustment %f exceeds cap %f", adjs[0].Percentage, maxAdjustmentPct)
	}
}

func TestGenerateAdjustments_NegativeAlpha(t *testing.T) {
	tagAlphas := []domain.TagAlpha{
		{Tag: "hidden", MeanAlpha: -0.00005, Variance: 0.00001, Count: 8},
	}

	adjs := GenerateAdjustments(tagAlphas)

	if len(adjs) != 1 {
		t.Fatalf("expected 1 adjustment, got %d", len(adjs))
	}
	if adjs[0].Direction != "decrease" {
		t.Errorf("expected decrease, got %s", adjs[0].Direction)
	}
}

func TestGenerateAdjustments_InsufficientData(t *testing.T) {
	tagAlphas := []domain.TagAlpha{
		{Tag: "pricing", MeanAlpha: 0.001, Count: 3}, // below minimum
	}

	adjs := GenerateAdjustments(tagAlphas)

	if len(adjs) != 0 {
		t.Errorf("expected no adjustments with insufficient data, got %d", len(adjs))
	}
}

func TestGenerateAdjustments_HighVarianceReducesAdjustment(t *testing.T) {
	tagAlphas := []domain.TagAlpha{
		{Tag: "pricing", MeanAlpha: 0.0002, Variance: 0.001, Count: 20}, // high variance
	}

	adjs := GenerateAdjustments(tagAlphas)

	if len(adjs) != 1 {
		t.Fatalf("expected 1 adjustment, got %d", len(adjs))
	}
	// High variance → halved adjustment (5% instead of 10%)
	if adjs[0].Percentage > 0.05+0.001 {
		t.Errorf("high variance should reduce adjustment, got %f", adjs[0].Percentage)
	}
}

func TestGenerateAdjustments_CappedAt10Pct(t *testing.T) {
	tagAlphas := []domain.TagAlpha{
		{Tag: "pricing", MeanAlpha: 1.0, Variance: 0.0, Count: 100}, // extreme alpha
	}

	adjs := GenerateAdjustments(tagAlphas)

	if len(adjs) != 1 {
		t.Fatalf("expected 1, got %d", len(adjs))
	}
	if adjs[0].Percentage > maxAdjustmentPct {
		t.Errorf("should be capped at %f, got %f", maxAdjustmentPct, adjs[0].Percentage)
	}
}
