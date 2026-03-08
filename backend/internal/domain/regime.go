package domain

import "time"

// RegimeType classifies the current market regime.
type RegimeType string

const (
	RegimeContango       RegimeType = "contango"       // demand > supply, rates rising
	RegimeBackwardation  RegimeType = "backwardation"  // supply > demand, rates falling
	RegimeNeutral        RegimeType = "neutral"        // balanced market
	RegimeCrisis         RegimeType = "crisis"         // extreme volatility / flash crash
)

// RegimeParams holds parameters describing the current market regime.
type RegimeParams struct {
	Volatility        float64       // current market volatility
	TrendStrength     float64       // -1 (strong downtrend) to +1 (strong uptrend)
	DemandSupplyRatio float64       // >1 means demand exceeds supply
	Duration          time.Duration // how long the current regime has lasted
}
