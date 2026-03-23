package strategy

import (
	"fmt"
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// M3: Proactive offer refresh — cancel offers that are both old and deep in queue
	refreshAge = 10 * time.Minute
)

// CompositeStrategy orchestrates all 13 strategy modules into a single
// pipeline following spec Appendix A Phase 4-5. It calls exported helper
// functions from each module in sequence to produce a unified DecisionResult.
type CompositeStrategy struct {
	now    func() time.Time
	preset domain.CurrencyPreset
}

// NewCompositeStrategyWithPreset creates a CompositeStrategy using a CurrencyPreset.
func NewCompositeStrategyWithPreset(preset domain.CurrencyPreset) *CompositeStrategy {
	return &CompositeStrategy{now: time.Now, preset: preset}
}

// NewCompositeStrategy creates a CompositeStrategy with stablecoin defaults (backward compatible).
func NewCompositeStrategy() *CompositeStrategy {
	return NewCompositeStrategyWithPreset(domain.StablecoinPreset)
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
	rate := ComputeBaseRateWithPreset(snap, cfg, c.preset)

	// M8: FRR feedback loop awareness — reduce FRR influence when market share is high
	if snap.OrderBook.AskDepth > 0 {
		marketShare := ctx.Available / snap.OrderBook.AskDepth
		if marketShare > 0.05 {
			frrReduction := (marketShare - 0.05) * 2.0
			if frrReduction > 0.5 {
				frrReduction = 0.5 // cap at 50% reduction
			}
			// Blend rate toward book mid rate
			bookRate := snap.OrderBook.MidRate
			if bookRate > 0 {
				rate = rate*(1-frrReduction) + bookRate*frrReduction
			}
		}
	}

	// 1b. Floor enforcement
	floorRate := ComputeFloorRate(snap, cfg, ctx.IdleMinutes)
	if rate < floorRate {
		rate = floorRate
	}

	// S10: Mean reversion adjusts floor tolerance
	if snap.RegimeParams.Volatility > 0 {
		ema7dProxy := snap.FRR * (1.0 + snap.FRRTrend*0.05)
		pHigher := PHigherRate(rate, ema7dProxy, snap.RegimeParams.Volatility, 1.0)
		if pHigher > 0.7 && ctx.IdleMinutes > 0 {
			// Rates likely to rise — reduce urgency discount by half
			adjustedFloor := ComputeFloorRate(snap, cfg, ctx.IdleMinutes*0.5)
			if adjustedFloor > floorRate {
				floorRate = adjustedFloor
				if rate < floorRate {
					rate = floorRate
				}
			}
		}
	}

	// 1c. Weekend premium
	ts := snap.Timestamp
	if ts.IsZero() {
		ts = c.now()
	}
	rate *= ComputeWeekendMultiplier(ts, snap.WeekendRatio)

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

	// S5: Cascade phase response
	if snap.CascadePhase == "late" {
		return &domain.DecisionResult{
			Cancels: staleCancels,
			Reason:  "composite:cascade_late",
		}
	}
	if snap.CascadePhase == "early" {
		period = cfg.Period.Min
	} else if snap.CascadePhase == "mid" {
		period = clampInt(14, cfg.Period.Min, cfg.Period.Max)
	}

	// Apply calendar constraint
	if calMaxPeriod > 0 && calMaxPeriod < period {
		period = clampInt(calMaxPeriod, cfg.Period.Min, period)
	}

	// Apply lockup period adjustment
	_, lockupPeriod := ComputeLockupPremium(rate, period, snap.RegimeParams.Volatility, snap.Regime)
	if lockupPeriod < period {
		period = clampInt(lockupPeriod, cfg.Period.Min, cfg.Period.Max)
	}

	// M2: Gap cost favors longer periods when gap time is significant
	if ctx.AvgGapMinutes > 0 {
		// Short periods have higher relative gap cost
		// gapCost2d = avgGap / (2 * 1440) vs gapCost30d = avgGap / (30 * 1440)
		gapCost := ctx.AvgGapMinutes / (float64(period) * 1440.0)
		if gapCost > 0.005 { // >0.5% downtime -> prefer longer period
			period = clampInt(period+2, cfg.Period.Min, cfg.Period.Max)
		}
	}

	// S4: Rate percentile influences period
	if snap.RatePercentile > 0.5 {
		// High percentile (> P75) -> extend period to lock in high rate
		extension := int(float64(cfg.Period.Max-period) * (snap.RatePercentile - 0.5) * 2)
		period = clampInt(period+extension, cfg.Period.Min, cfg.Period.Max)
	} else if snap.RatePercentile < -0.5 {
		// Low percentile (< P25) -> shorten period, wait for recovery
		reduction := int(float64(period-cfg.Period.Min) * (-snap.RatePercentile - 0.5) * 2)
		period = clampInt(period-reduction, cfg.Period.Min, cfg.Period.Max)
	}

	// Stagger: adjust period to reduce expiry concentration
	period = AdjustPeriodForStagger(period, ctx.ActiveCredits, cfg, c.now())

	// ── Stage 3: Offer Structure ──

	// S8: Confidence-scaled deployment — deploy less when signals are contradictory
	deployRatio := computeDeploymentRatio(snap)

	// S4: Low percentile further reduces deployment
	if snap.RatePercentile < -0.5 {
		deployRatio *= 0.8 // reduce by 20% when rates are historically low
	}

	available *= deployRatio

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

	// 4c. Collect cancels (stale + residuals + refresh)
	residualCancels := DetectResiduals(ctx.ActiveOffers)

	// M3: Proactive offer refresh — cancel old offers stuck deep in queue
	var refreshCancels []int64
	for _, o := range ctx.ActiveOffers {
		if !o.CreatedAt.IsZero() && time.Since(o.CreatedAt) > refreshAge {
			if ComputeQueueDiscount(o.Rate, snap.OrderBook) < 1.0 {
				refreshCancels = append(refreshCancels, o.ID)
			}
		}
	}

	var allCancels []int64
	allCancels = append(allCancels, staleCancels...)
	allCancels = append(allCancels, residualCancels...)
	allCancels = append(allCancels, refreshCancels...)
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

// computeDeploymentRatio returns the fraction of capital to deploy based on signal confidence.
// Range: [0.5, 1.0]. Low confidence → deploy 50%. High confidence + strong MDC → deploy ~100%.
func computeDeploymentRatio(snap *domain.MarketSnapshot) float64 {
	if len(snap.Signals) == 0 {
		return 0.5
	}
	var totalConf float64
	for _, s := range snap.Signals {
		totalConf += s.Confidence
	}
	avgConf := totalConf / float64(len(snap.Signals))
	ratio := 0.5 + 0.5*math.Abs(snap.MDC.Score)*avgConf
	if ratio > 1.0 {
		return 1.0
	}
	return ratio
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
