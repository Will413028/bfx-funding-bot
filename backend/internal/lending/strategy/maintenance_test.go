package strategy

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestMaintenance_QueueHeadKeep(t *testing.T) {
	m := NewMaintenanceStrategy()
	offer := domain.FundingOffer{Rate: 0.00025, Period: 7}
	if !m.ShouldKeepOffer(offer, 0.00025, 7) {
		t.Error("should keep offer with matching rate and period")
	}
}

func TestMaintenance_QueueHeadReplace_RateDiff(t *testing.T) {
	m := NewMaintenanceStrategy()
	offer := domain.FundingOffer{Rate: 0.00025, Period: 7}
	if m.ShouldKeepOffer(offer, 0.00030, 7) {
		t.Error("should replace offer with different rate")
	}
}

func TestMaintenance_QueueHeadReplace_PeriodDiff(t *testing.T) {
	m := NewMaintenanceStrategy()
	offer := domain.FundingOffer{Rate: 0.00025, Period: 7}
	if m.ShouldKeepOffer(offer, 0.00025, 14) {
		t.Error("should replace offer with different period")
	}
}

func TestMaintenance_ZombieOffers_Active(t *testing.T) {
	m := NewMaintenanceStrategy()
	now := time.Now()
	offers := []domain.FundingOffer{
		{ID: 1, Status: "ACTIVE", CreatedAt: now.Add(-5 * time.Minute)},  // fresh
		{ID: 2, Status: "ACTIVE", CreatedAt: now.Add(-15 * time.Minute)}, // zombie (>12min active)
		{ID: 3, Status: "ACTIVE", CreatedAt: now.Add(-60 * time.Minute)}, // zombie
	}
	zombies := m.ZombieOffers(offers, now, true)
	if len(zombies) != 2 {
		t.Errorf("expected 2 zombies in active market, got %d", len(zombies))
	}
}

func TestMaintenance_ZombieOffers_DeadWater(t *testing.T) {
	m := NewMaintenanceStrategy()
	now := time.Now()
	offers := []domain.FundingOffer{
		{ID: 1, Status: "ACTIVE", CreatedAt: now.Add(-15 * time.Minute)}, // fresh (< 52min)
		{ID: 2, Status: "ACTIVE", CreatedAt: now.Add(-55 * time.Minute)}, // zombie (> 52min)
	}
	zombies := m.ZombieOffers(offers, now, false)
	if len(zombies) != 1 || zombies[0] != 2 {
		t.Errorf("expected zombie ID 2, got %v", zombies)
	}
}

func TestMaintenance_AtomicSwap(t *testing.T) {
	m := NewMaintenanceStrategy()
	if !m.ShouldAtomicSwap(10000, 5000) {
		t.Error("should allow atomic swap when balance >= new order")
	}
	if m.ShouldAtomicSwap(3000, 5000) {
		t.Error("should not allow atomic swap when balance < new order")
	}
}
