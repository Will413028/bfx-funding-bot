package domain

import "time"

// SignalType identifies a market signal source.
type SignalType string

const (
	SignalMDC                  SignalType = "mdc"
	SignalBookConsumption      SignalType = "book_consumption"
	SignalLiquidationCascade   SignalType = "liquidation_cascade"
	SignalMomentum             SignalType = "momentum"
	SignalMarginUsage          SignalType = "margin_usage"
	SignalCrossCurrency        SignalType = "cross_currency"
	SignalIntraday             SignalType = "intraday"
)

// SignalValue represents the result of a single signal computation.
type SignalValue struct {
	Type       SignalType // which signal source produced this
	Value      float64   // normalized value, -1 (bearish) to +1 (bullish)
	Confidence float64   // 0 (no confidence) to 1 (full confidence)
	Timestamp  time.Time
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
	Score          float64   // weighted composite of all signals
	DemandPressure float64  // aggregate demand-side pressure
	SupplyPressure float64  // aggregate supply-side pressure
	Timestamp      time.Time
}
