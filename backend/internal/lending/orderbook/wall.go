package orderbook

import (
	"math"
	"sort"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	defaultWallThreshold       = 0.05 // 5% of total side depth
	defaultClusterSpreadFactor = 2.0  // adjacent if rate diff < spread × factor
)

// WallOptions configures wall detection.
type WallOptions struct {
	Threshold           float64 // min fraction of side depth to be a wall (default 0.05)
	ClusterSpreadFactor float64 // adjacency factor relative to spread (default 2.0)
}

// DetectWalls finds single and distributed walls in the order book.
// spread is the current bid-ask spread (used for clustering adjacency).
func DetectWalls(entries []domain.BookEntry, spread float64, opts *WallOptions) []domain.WallPosition {
	if len(entries) == 0 {
		return nil
	}

	threshold := defaultWallThreshold
	clusterFactor := defaultClusterSpreadFactor
	if opts != nil {
		if opts.Threshold > 0 {
			threshold = opts.Threshold
		}
		if opts.ClusterSpreadFactor > 0 {
			clusterFactor = opts.ClusterSpreadFactor
		}
	}

	// Calculate total depth per side
	var totalOffer, totalBid float64
	for _, e := range entries {
		if e.Amount > 0 {
			totalOffer += e.Amount
		} else {
			totalBid += math.Abs(e.Amount)
		}
	}

	var walls []domain.WallPosition

	// Detect single walls
	singleWallRates := make(map[float64]bool)
	for _, e := range entries {
		if e.Amount > 0 && totalOffer > 0 {
			if e.Amount/totalOffer >= threshold {
				walls = append(walls, domain.WallPosition{
					Rate:       e.Rate,
					Amount:     e.Amount,
					Side:       "offer",
					EntryCount: e.Count,
					Type:       domain.WallSingle,
				})
				singleWallRates[e.Rate] = true
			}
		} else if e.Amount < 0 && totalBid > 0 {
			amt := math.Abs(e.Amount)
			if amt/totalBid >= threshold {
				walls = append(walls, domain.WallPosition{
					Rate:       e.Rate,
					Amount:     amt,
					Side:       "bid",
					EntryCount: e.Count,
					Type:       domain.WallSingle,
				})
				singleWallRates[e.Rate] = true
			}
		}
	}

	// Detect distributed walls (clusters of adjacent entries)
	if spread > 0 {
		maxGap := spread * clusterFactor
		offerWalls := detectDistributedSide(entries, totalOffer, threshold, maxGap, true, singleWallRates)
		bidWalls := detectDistributedSide(entries, totalBid, threshold, maxGap, false, singleWallRates)
		walls = append(walls, offerWalls...)
		walls = append(walls, bidWalls...)
	}

	return walls
}

func detectDistributedSide(
	entries []domain.BookEntry,
	totalDepth float64,
	threshold float64,
	maxGap float64,
	isOffer bool,
	singleWallRates map[float64]bool,
) []domain.WallPosition {
	if totalDepth == 0 {
		return nil
	}

	// Collect and sort entries for this side
	type entry struct {
		rate   float64
		amount float64
		count  int
	}
	var side []entry
	for _, e := range entries {
		if isOffer && e.Amount > 0 {
			if !singleWallRates[e.Rate] {
				side = append(side, entry{rate: e.Rate, amount: e.Amount, count: e.Count})
			}
		} else if !isOffer && e.Amount < 0 {
			if !singleWallRates[e.Rate] {
				side = append(side, entry{rate: e.Rate, amount: math.Abs(e.Amount), count: e.Count})
			}
		}
	}

	if len(side) < 2 {
		return nil
	}

	sort.Slice(side, func(i, j int) bool { return side[i].rate < side[j].rate })

	// Cluster adjacent entries
	var walls []domain.WallPosition
	clusterStart := 0
	for i := 1; i <= len(side); i++ {
		// End of cluster: gap too large or end of slice
		if i == len(side) || side[i].rate-side[i-1].rate > maxGap {
			// Check cluster [clusterStart, i)
			if i-clusterStart >= 2 {
				var totalAmt float64
				var totalCount int
				var rateSum float64
				for j := clusterStart; j < i; j++ {
					totalAmt += side[j].amount
					totalCount += side[j].count
					rateSum += side[j].rate
				}
				if totalAmt/totalDepth >= threshold {
					sideStr := "offer"
					if !isOffer {
						sideStr = "bid"
					}
					walls = append(walls, domain.WallPosition{
						Rate:       rateSum / float64(i-clusterStart),
						Amount:     totalAmt,
						Side:       sideStr,
						EntryCount: totalCount,
						Type:       domain.WallDistributed,
					})
				}
			}
			clusterStart = i
		}
	}

	return walls
}
