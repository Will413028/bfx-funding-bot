package domain

import "time"

// SignalType identifies a market signal source.
type SignalType string

const (
	SignalMDC                SignalType = "mdc"
	SignalBookConsumption    SignalType = "book_consumption"
	SignalLiquidationCascade SignalType = "liquidation_cascade"
	SignalMomentum           SignalType = "momentum"
	SignalMarginUsage        SignalType = "margin_usage"
	SignalCrossCurrency      SignalType = "cross_currency"
	SignalIntraday           SignalType = "intraday"
	SignalFRRTrend           SignalType = "frr_trend"
	SignalRateSpike          SignalType = "rate_spike"
)

// SignalValue represents the result of a single signal computation.
type SignalValue struct {
	Timestamp  time.Time
	Type       SignalType
	Value      float64
	Confidence float64
}

// SignalHealthState represents the health state of a signal source.
type SignalHealthState string

const (
	SignalHealthy    SignalHealthState = "healthy"
	SignalWarning    SignalHealthState = "warning"
	SignalDegraded   SignalHealthState = "degraded"
	SignalRecovering SignalHealthState = "recovering"
)

// SignalHealthSummary maps each signal source to its health state.
type SignalHealthSummary map[SignalType]SignalHealthState

// MDCResult represents the Market Demand Curve computation result.
// MDC aggregates multiple signal sources into a weighted composite score.
type MDCResult struct {
	Timestamp      time.Time
	Score          float64
	DemandPressure float64
	SupplyPressure float64
}
