package domain

import "time"

// PerformanceRecord captures per-execution performance data (§9.1).
type PerformanceRecord struct {
	UserID       string
	ActualRate   float64
	FRRAtTime    float64
	Alpha        float64 // ActualRate - FRRAtTime
	MDCScore     float64
	Regime       RegimeType
	StrategyTags []string
	Amount       float64
	Period       int
	Currency     string
	Timestamp    time.Time
}

// AlphaSummary aggregates alpha statistics over a rolling window.
type AlphaSummary struct {
	MeanAlpha  float64
	TotalAlpha float64
	Count      int
	ByRegime   map[RegimeType]RegimeAlpha
}

// RegimeAlpha holds per-regime alpha statistics.
type RegimeAlpha struct {
	MeanAlpha float64
	Count     int
}

// TagAlpha holds per-strategy-tag alpha statistics.
type TagAlpha struct {
	Tag       string
	MeanAlpha float64
	Variance  float64
	Count     int
	Regime    RegimeType // empty string = all regimes combined
}

// AdaptiveAdjustment represents a recommended parameter change (§9.3).
type AdaptiveAdjustment struct {
	Tag        string     // strategy module tag
	Regime     RegimeType // specific regime, or empty for global
	Direction  string     // "increase" or "decrease"
	Percentage float64    // 0.05–0.10 (5%–10%)
	Reason     string     // human-readable explanation
}
