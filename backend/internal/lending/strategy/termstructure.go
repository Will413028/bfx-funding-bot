package strategy

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// TermStructureShape classifies the yield curve shape (§5.1).
type TermStructureShape string

const (
	TermSteep    TermStructureShape = "steep"    // normal: longer period = higher rate
	TermHumped   TermStructureShape = "humped"   // mid-term rates peak (7-14d)
	TermInverted TermStructureShape = "inverted" // shorter period = higher rate
	TermFlat     TermStructureShape = "flat"     // rates nearly equal across periods

	// Threshold for considering rates "nearly equal" (flat curve)
	flatThreshold = 0.000005 // 0.0005% daily
)

// TermStructureAnalyzer classifies the yield curve and acts as a pre-filter
// for period selection decisions.
type TermStructureAnalyzer struct{}

func NewTermStructureAnalyzer() *TermStructureAnalyzer {
	return &TermStructureAnalyzer{}
}

// ClassifyFromBook extracts representative rates from the order book for
// short (2d), mid (14d), and long (30d) periods, then classifies the curve shape.
func (a *TermStructureAnalyzer) ClassifyFromBook(book []domain.BookEntry) TermStructureShape {
	shortRate, midRate, longRate := extractPeriodRates(book)

	if shortRate == 0 && midRate == 0 && longRate == 0 {
		return TermFlat // no data → conservative
	}

	return classify(shortRate, midRate, longRate)
}

// FilterPeriod adjusts min/max period based on term structure shape.
// Returns adjusted (min, max) period bounds.
func (a *TermStructureAnalyzer) FilterPeriod(shape TermStructureShape, cfgMin, cfgMax int) (int, int) {
	switch shape {
	case TermInverted, TermFlat:
		// Force short-term: 2 days
		return cfgMin, cfgMin
	case TermHumped:
		// Concentrate on 7-14 days
		lo := max(cfgMin, 7)
		hi := min(cfgMax, 14)
		if lo > hi {
			return cfgMin, cfgMax // fallback if config range doesn't support 7-14
		}
		return lo, hi
	default: // TermSteep
		// Full range allowed
		return cfgMin, cfgMax
	}
}

func classify(shortRate, midRate, longRate float64) TermStructureShape {
	slope := longRate - shortRate

	// Flat: all rates nearly equal
	if math.Abs(slope) < flatThreshold && math.Abs(midRate-shortRate) < flatThreshold {
		return TermFlat
	}

	// Humped: mid-term rate exceeds both short and long
	if midRate > shortRate && midRate > longRate {
		return TermHumped
	}

	// Inverted: short-term rate higher than long-term
	if slope < -flatThreshold {
		return TermInverted
	}

	// Normal steep: longer = higher
	return TermSteep
}

// extractPeriodRates computes average ask rates for short (≤3d), mid (7-14d), long (≥28d).
func extractPeriodRates(book []domain.BookEntry) (short, mid, long float64) {
	var shortSum, midSum, longSum float64
	var shortN, midN, longN int

	for _, e := range book {
		if e.Amount <= 0 { // only ask/offer side
			continue
		}
		switch {
		case e.Period <= 3:
			shortSum += e.Rate
			shortN++
		case e.Period >= 7 && e.Period <= 14:
			midSum += e.Rate
			midN++
		case e.Period >= 28:
			longSum += e.Rate
			longN++
		}
	}

	if shortN > 0 {
		short = shortSum / float64(shortN)
	}
	if midN > 0 {
		mid = midSum / float64(midN)
	}
	if longN > 0 {
		long = longSum / float64(longN)
	}
	return
}
