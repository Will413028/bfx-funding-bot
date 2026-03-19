package strategy

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Default expected lending duration (hours) for EV calculation
	defaultExpectedDurationHours = 48.0 // 2 days typical short loan

	// MDC slope lookback window for P(higher_rate) estimation
	mdcSlopeLookbackHeartbeats = 10

	// Safety valve: after this many idle hours, reduce floor by 20% to force deployment
	safetyValveIdleHours = 2.0
	safetyValveDiscount  = 0.80

	// Minimum P(higher_rate) — always some uncertainty
	minPHigher = 0.05
	// Maximum P(higher_rate) — never fully certain
	maxPHigher = 0.85
)

// OpportunityCostStrategy implements the EV_deploy vs EV_wait framework (§4.8).
// It decides whether to deploy funds now or wait for better rates.
type OpportunityCostStrategy struct {
	// lastIdleSince tracks when the strategy started recommending "wait".
	// Reset when deploy is recommended.
	lastIdleSince *time.Time

	// mdcHistory tracks recent MDC scores for slope estimation
	mdcHistory []mdcPoint
}

type mdcPoint struct {
	score float64
	ts    time.Time
}

func NewOpportunityCostStrategy() *OpportunityCostStrategy {
	return &OpportunityCostStrategy{}
}

// Evaluate computes EV_deploy vs EV_wait and returns the decision.
// currentRate: the rate computed by pricing strategy (post all adjustments).
// opportunityCostPerHour: historical average hourly earning per unit of capital.
// Returns: shouldDeploy bool, reason string.
func (s *OpportunityCostStrategy) Evaluate(
	currentRate float64,
	snap *domain.MarketSnapshot,
	opportunityCostPerHour float64,
	now time.Time,
) (shouldDeploy bool, reason string) {
	// Track MDC for slope estimation
	s.recordMDC(snap.MDC.Score, now)

	// EV_deploy = current_rate × expected_duration_hours
	evDeploy := currentRate * defaultExpectedDurationHours

	// Estimate P(higher_rate) from MDC slope
	pHigher := s.estimatePHigherRate()

	// Expected higher rate: assume 20% above current if rates rise
	expectedHigherRate := currentRate * 1.20

	// Idle hours since last deploy recommendation
	idleHours := s.idleHours(now)

	// EV_wait = P(higher) × higher_rate × duration - idle_hours × opportunity_cost
	evWait := pHigher*expectedHigherRate*defaultExpectedDurationHours - idleHours*opportunityCostPerHour

	if evDeploy >= evWait {
		// Deploy: reset idle tracking
		s.lastIdleSince = nil
		return true, "ev_deploy"
	}

	// Wait: start tracking idle time
	if s.lastIdleSince == nil {
		t := now
		s.lastIdleSince = &t
	}

	// Safety valve: if idle > 2 hours, force deploy (floor will be discounted)
	if idleHours > safetyValveIdleHours {
		s.lastIdleSince = nil
		return true, "ev_safety_valve"
	}

	return false, "ev_wait"
}

// IdleFloorDiscount returns a discount factor for the rate floor when idle too long.
// Returns 1.0 normally, 0.80 if idle > 2 hours (§4.9 safety valve).
func (s *OpportunityCostStrategy) IdleFloorDiscount(now time.Time) float64 {
	if s.idleHours(now) > safetyValveIdleHours {
		return safetyValveDiscount
	}
	return 1.0
}

func (s *OpportunityCostStrategy) idleHours(now time.Time) float64 {
	if s.lastIdleSince == nil {
		return 0
	}
	return now.Sub(*s.lastIdleSince).Hours()
}

func (s *OpportunityCostStrategy) recordMDC(score float64, now time.Time) {
	s.mdcHistory = append(s.mdcHistory, mdcPoint{score: score, ts: now})

	// Keep only last N points
	if len(s.mdcHistory) > mdcSlopeLookbackHeartbeats*2 {
		s.mdcHistory = s.mdcHistory[len(s.mdcHistory)-mdcSlopeLookbackHeartbeats:]
	}
}

// estimatePHigherRate estimates the probability of rates rising based on MDC trend.
// Positive MDC slope → higher probability; negative → lower.
func (s *OpportunityCostStrategy) estimatePHigherRate() float64 {
	if len(s.mdcHistory) < 2 {
		return 0.5 // insufficient data → neutral
	}

	// Simple linear slope of recent MDC scores
	n := len(s.mdcHistory)
	recent := s.mdcHistory[n-1]
	older := s.mdcHistory[0]

	dt := recent.ts.Sub(older.ts).Seconds()
	if dt <= 0 {
		return 0.5
	}

	slope := (recent.score - older.score) / dt

	// Map slope to probability: positive slope → higher P, negative → lower P
	// Sigmoid-like mapping centered at 0.5
	// slope of ±0.001/sec ≈ meaningful change
	p := 0.5 + slope*500 // scale factor: 0.001/sec → ±0.5 shift

	return math.Max(minPHigher, math.Min(maxPHigher, p))
}

// Apply implements the Strategy interface for standalone use.
func (s *OpportunityCostStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	if ctx.Snapshot == nil {
		return &domain.DecisionResult{Reason: "no_snapshot"}
	}
	if ctx.Snapshot.FlashFreeze {
		return &domain.DecisionResult{Reason: "flash_freeze"}
	}
	if ctx.Available < minBalance {
		return &domain.DecisionResult{Reason: "insufficient_balance"}
	}

	// Use FRR as current rate proxy for standalone evaluation
	currentRate := ctx.Snapshot.FRR
	if currentRate <= 0 {
		currentRate = ctx.Config.Rate.Min
	}

	// Simple opportunity cost: config min rate as proxy for historical earnings
	oppCostPerHour := ctx.Config.Rate.Min / 24.0

	shouldDeploy, reason := s.Evaluate(currentRate, ctx.Snapshot, oppCostPerHour, ctx.Snapshot.Timestamp)

	if !shouldDeploy {
		return &domain.DecisionResult{Reason: "floor:" + reason}
	}

	return &domain.DecisionResult{
		Offers: []domain.OfferDecision{
			{
				Amount: math.Min(ctx.Available, ctx.Config.Amount.Max),
				Rate:   currentRate,
				Period: ctx.Config.Period.Min,
			},
		},
		Reason: reason,
	}
}
