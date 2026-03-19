package strategy

import (
	"math"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Queue head reservation: rate match tolerance (§7.1)
	queueHeadEpsilon = 0.00000002

	// Zombie order TTL thresholds (§7.1)
	zombieTTLActive    = 12 * time.Minute // active period (avg of 10-15 min)
	zombieTTLDeadWater = 52 * time.Minute // dead water period (avg of 45-60 min)
)

// MaintenanceStrategy handles queue head reservation, zombie order cleanup,
// and atomic order swap decisions (§7.1).
type MaintenanceStrategy struct{}

func NewMaintenanceStrategy() *MaintenanceStrategy {
	return &MaintenanceStrategy{}
}

// ShouldKeepOffer returns true if an existing offer matches the new target
// closely enough that replacing it would lose queue position.
func (m *MaintenanceStrategy) ShouldKeepOffer(existing domain.FundingOffer, targetRate float64, targetPeriod int) bool {
	if existing.Period != targetPeriod {
		return false
	}
	return math.Abs(existing.Rate-targetRate) < queueHeadEpsilon
}

// ZombieOffers identifies stale offers that should be cancelled based on TTL.
// isActive indicates whether the market is in an active or dead water period.
func (m *MaintenanceStrategy) ZombieOffers(offers []domain.FundingOffer, now time.Time, isActive bool) []int64 {
	ttl := zombieTTLDeadWater
	if isActive {
		ttl = zombieTTLActive
	}

	var zombies []int64
	for _, o := range offers {
		age := now.Sub(o.CreatedAt)
		if age > ttl && o.Status == "ACTIVE" {
			zombies = append(zombies, o.ID)
		}
	}
	return zombies
}

// ShouldAtomicSwap returns true if there's enough balance to place a new order
// before cancelling the old one (maintaining market presence).
func (m *MaintenanceStrategy) ShouldAtomicSwap(available, newOrderAmount float64) bool {
	return available >= newOrderAmount
}

// Apply evaluates active offers for maintenance actions.
func (m *MaintenanceStrategy) Apply(ctx *domain.DecisionContext) *domain.DecisionResult {
	if ctx.Snapshot == nil {
		return &domain.DecisionResult{Reason: "no_snapshot"}
	}
	if ctx.Snapshot.FlashFreeze {
		return &domain.DecisionResult{Reason: "flash_freeze"}
	}
	if ctx.Available < minBalance {
		return &domain.DecisionResult{Reason: "insufficient_balance"}
	}

	// Determine if market is active (use volatility as proxy)
	isActive := ctx.Snapshot.RegimeParams.Volatility > 0.05

	// Find zombie orders
	now := ctx.Snapshot.Timestamp
	zombies := m.ZombieOffers(ctx.ActiveOffers, now, isActive)

	return &domain.DecisionResult{
		Cancels: zombies,
		Reason:  "maintenance",
	}
}
