package orderbook

import (
	"math"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	defaultSnapshotHistorySize = 10
	defaultFollowThreshold     = 0.60
	defaultRoundThreshold      = 0.40
	followWeight               = 0.6
	roundWeight                = 0.4
	followProximityBps         = 1.0 // within 1 bps of best ask
)

// CompetitorDetector tracks order book patterns to detect automated competitor activity.
type CompetitorDetector struct {
	history     []bookSnapshot
	historySize int
}

type bookSnapshot struct {
	bestAsk   float64
	offerRates map[float64]bool // set of offer rates
}

func NewCompetitorDetector() *CompetitorDetector {
	return &CompetitorDetector{
		historySize: defaultSnapshotHistorySize,
	}
}

// Analyze processes a book snapshot and returns a composite competitor activity score [0,1].
func (c *CompetitorDetector) Analyze(entries []domain.BookEntry) float64 {
	// Build current snapshot
	current := bookSnapshot{
		offerRates: make(map[float64]bool),
	}
	bestAsk := math.MaxFloat64
	for _, e := range entries {
		if e.Amount > 0 {
			current.offerRates[e.Rate] = true
			if e.Rate < bestAsk {
				bestAsk = e.Rate
			}
		}
	}
	if bestAsk == math.MaxFloat64 {
		bestAsk = 0
	}
	current.bestAsk = bestAsk

	c.history = append(c.history, current)
	if len(c.history) > c.historySize {
		c.history = c.history[len(c.history)-c.historySize:]
	}

	// Need at least 2 snapshots
	if len(c.history) < 2 {
		return 0
	}

	followRate := c.computeFollowRate()
	roundRatio := computeRoundNumberRatio(entries)

	return followWeight*followRate + roundWeight*roundRatio
}

// computeFollowRate checks how often new entries appear near the best ask
// between consecutive snapshots.
func (c *CompetitorDetector) computeFollowRate() float64 {
	followCount := 0
	comparisons := 0

	for i := 1; i < len(c.history); i++ {
		prev := c.history[i-1]
		curr := c.history[i]

		if curr.bestAsk == 0 {
			continue
		}
		comparisons++

		// Check for new rates near current best ask
		proximityThreshold := curr.bestAsk * followProximityBps / 10000
		if proximityThreshold == 0 {
			proximityThreshold = 1e-8
		}

		for rate := range curr.offerRates {
			if !prev.offerRates[rate] {
				// New entry — check if near best ask
				if math.Abs(rate-curr.bestAsk) <= proximityThreshold {
					followCount++
					break // one match per snapshot pair is enough
				}
			}
		}
	}

	if comparisons == 0 {
		return 0
	}
	return float64(followCount) / float64(comparisons)
}

// computeRoundNumberRatio measures the proportion of offer entries at
// round-number rates (integer basis points).
func computeRoundNumberRatio(entries []domain.BookEntry) float64 {
	var total, round int
	for _, e := range entries {
		if e.Amount > 0 {
			total++
			// Check if rate is an integer number of bps (0.01% = 0.0001)
			bps := e.Rate * 10000 // convert to bps
			if math.Abs(bps-math.Round(bps)) < 1e-6 {
				round++
			}
		}
	}
	if total == 0 {
		return 0
	}
	return float64(round) / float64(total)
}
