package strategy

import (
	"fmt"
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// CompositeStrategy orchestrates all 13 strategy modules into a single
// pipeline following spec Appendix A Phase 4-5. It calls exported helper
// functions from each module in sequence to produce a unified DecisionResult.
type CompositeStrategy struct {
	now func() time.Time
}

// NewCompositeStrategy creates a new CompositeStrategy.
func NewCompositeStrategy() *CompositeStrategy {
	return &CompositeStrategy{now: time.Now}
}

// Apply executes the four-stage pipeline:
//  1. Rate Resolution
//  2. Period Resolution
//  3. Offer Structure (allocation + splitting)
//  4. Final Adjustments (noise + partial fill + cancels)
func (c *CompositeStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	// ── Guard checks ──
	if ctx.Snapshot == nil {
		return &domain.DecisionResult{Reason: "composite:no_snapshot"}
	}
	if ctx.Snapshot.FlashFreeze {
		return &domain.DecisionResult{Reason: "composite:flash_freeze"}
	}
	if ctx.Available < minBalance {
		return &domain.DecisionResult{Reason: "composite:low_balance"}
	}

	snap := ctx.Snapshot
	cfg := ctx.Config

	// ── Stage 1: Rate Resolution ──

	// 1a. Base rate from pricing (FRR + MDC + regime + depth + walls)
	rate := ComputeBaseRate(snap, cfg)

	// 1b. Floor enforcement
	floorRate := ComputeFloorRate(snap, cfg)
	if rate < floorRate {
		rate = floorRate
	}

	// 1c. Weekend premium
	ts := snap.Timestamp
	if ts.IsZero() {
		ts = c.now()
	}
	rate *= ComputeWeekendMultiplier(ts)

	// 1d. Calendar event premium (may constrain period)
	calMul, calMaxPeriod := ComputeCalendarMultiplier(ts)
	rate *= calMul

	// 1e. Lockup cost premium (may shorten period)
	lockupPremium, _ := ComputeLockupPremium(rate, cfg.Period.Max, snap.RegimeParams.Volatility, snap.Regime)
	rate += lockupPremium

	// 1f. Queue position discount + stale cancels
	queueMul := ComputeQueueDiscount(rate, snap.OrderBook)
	rate *= queueMul
	staleCancels := DetectStaleOffers(ctx.ActiveOffers, snap.OrderBook)

	// 1g. Market impact assessment
	available := math.Min(ctx.Available, cfg.Amount.Max)
	existingAmount := 0.0
	for _, o := range ctx.ActiveOffers {
		existingAmount += o.Amount
	}
	impactMul, maxAmount := ComputeImpactMultiplier(available, existingAmount, snap.OrderBook.AskDepth)
	rate *= impactMul
	if maxAmount <= 0 {
		return &domain.DecisionResult{
			Cancels: staleCancels,
			Reason:  "composite:impact_over_limit",
		}
	}
	if available > maxAmount {
		available = maxAmount
	}

	// 1h. Clamp rate to config bounds
	rate = clamp(rate, cfg.Rate.Min, cfg.Rate.Max)

	// ── Stage 2: Period Resolution ──

	period := ComputePeriod(snap.Regime, snap.RegimeParams.Volatility, rate, snap.FRR, cfg)

	// Apply calendar constraint
	if calMaxPeriod > 0 && calMaxPeriod < period {
		period = clampInt(calMaxPeriod, cfg.Period.Min, period)
	}

	// Apply lockup period adjustment
	_, lockupPeriod := ComputeLockupPremium(rate, period, snap.RegimeParams.Volatility, snap.Regime)
	if lockupPeriod < period {
		period = clampInt(lockupPeriod, cfg.Period.Min, cfg.Period.Max)
	}

	// Stagger: adjust period to reduce expiry concentration
	period = AdjustPeriodForStagger(period, ctx.ActiveCredits, cfg, c.now())

	// ── Stage 3: Offer Structure ──

	tiers := ComputeTiers(available, snap.Regime)
	var offers []domain.OfferDecision

	for _, t := range tiers {
		tierAmount := available * t.Ratio
		tierRate := clamp(rate*t.RateMultiplier, cfg.Rate.Min, cfg.Rate.Max)

		// Depth-aware splitting
		splitCount := ComputeSplits(tierAmount, snap.OrderBook.AskDepth)
		perSplit := tierAmount / float64(splitCount)

		for i := 0; i < splitCount; i++ {
			offers = append(offers, domain.OfferDecision{
				Amount: perSplit,
				Rate:   tierRate,
				Period: period,
			})
		}
	}

	// ── Stage 4: Final Adjustments ──

	// 4a. Noise perturbation
	filtered := offers[:0]
	for _, o := range offers {
		noisyRate, noisyAmount := ApplyNoise(o.Rate, o.Amount, cfg.Rate.Min, cfg.Rate.Max)
		// Cap amount to available
		noisyAmount = math.Min(noisyAmount, ctx.Available)
		if noisyAmount < minBalance {
			continue // remove offers below minimum
		}
		filtered = append(filtered, domain.OfferDecision{
			Amount: noisyAmount,
			Rate:   noisyRate,
			Period: o.Period,
		})
	}
	offers = filtered

	// 4b. Partial fill adjustment (amount scaling based on fill activity)
	for i := range offers {
		offers[i].Amount = ComputeFillAdjustment(offers[i].Amount, ctx.ActiveOffers, cfg)
		offers[i].Amount = math.Min(offers[i].Amount, ctx.Available)
		offers[i].Amount = math.Min(offers[i].Amount, cfg.Amount.Max)
	}

	// Remove any offers that fell below minimum after adjustment
	filtered = offers[:0]
	for _, o := range offers {
		if o.Amount >= minBalance {
			filtered = append(filtered, o)
		}
	}
	offers = filtered

	// 4c. Collect cancels (stale + residuals)
	residualCancels := DetectResiduals(ctx.ActiveOffers)
	var allCancels []int64
	allCancels = append(allCancels, staleCancels...)
	allCancels = append(allCancels, residualCancels...)
	allCancels = dedup(allCancels)

	// ── Build result ──
	reason := fmt.Sprintf("composite:rate=%.7f|period=%d|tiers=%d|offers=%d",
		rate, period, len(tiers), len(offers))

	return &domain.DecisionResult{
		Offers:  offers,
		Cancels: allCancels,
		Reason:  reason,
	}
}

// dedup removes duplicate int64 values from a slice.
func dedup(ids []int64) []int64 {
	if len(ids) == 0 {
		return nil
	}
	seen := make(map[int64]struct{}, len(ids))
	result := make([]int64, 0, len(ids))
	for _, id := range ids {
		if _, ok := seen[id]; !ok {
			seen[id] = struct{}{}
			result = append(result, id)
		}
	}
	return result
}
