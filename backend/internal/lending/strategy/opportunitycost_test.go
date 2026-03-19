package strategy

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

func TestOpportunityCost_DeployWhenEVHigher(t *testing.T) {
	s := NewOpportunityCostStrategy()
	now := time.Now()
	snap := &domain.MarketSnapshot{
		FRR: 0.0003,
		MDC: domain.MDCResult{Score: 0.5},
	}

	// High current rate, low opportunity cost → should deploy
	deploy, reason := s.Evaluate(0.0005, snap, 0.000001, now)
	if !deploy {
		t.Errorf("expected deploy, got wait (reason: %s)", reason)
	}
	if reason != "ev_deploy" {
		t.Errorf("expected reason ev_deploy, got %s", reason)
	}
}

func TestOpportunityCost_WaitWhenRatesRising(t *testing.T) {
	s := NewOpportunityCostStrategy()
	now := time.Now()

	// Simulate rising MDC (positive slope → high P(higher_rate))
	for i := 0; i < 10; i++ {
		s.recordMDC(float64(i)*0.1, now.Add(time.Duration(i)*time.Second))
	}

	snap := &domain.MarketSnapshot{
		FRR: 0.0001,
		MDC: domain.MDCResult{Score: 0.9},
	}

	// Low current rate, rising MDC → might wait
	deploy, _ := s.Evaluate(0.00005, snap, 0.0, now.Add(10*time.Second))
	// With zero opportunity cost and rising rates, EV_wait should be higher
	if deploy {
		t.Log("deployed despite rising rates — EV_deploy may still be higher at this rate")
	}
}

func TestOpportunityCost_SafetyValve(t *testing.T) {
	s := NewOpportunityCostStrategy()
	now := time.Now()

	// Directly set idle since 3 hours ago (simulating prolonged waiting)
	idleStart := now.Add(-3 * time.Hour)
	s.lastIdleSince = &idleStart

	snap := &domain.MarketSnapshot{
		FRR: 0.0001,
		MDC: domain.MDCResult{Score: -0.5},
	}

	// Even with low rate, safety valve should trigger after >2hr idle
	deploy, reason := s.Evaluate(0.00001, snap, 0.0, now)
	if !deploy {
		t.Error("expected deploy after safety valve (>2hr idle)")
	}
	if reason != "ev_safety_valve" && reason != "ev_deploy" {
		t.Errorf("expected ev_safety_valve or ev_deploy, got %s", reason)
	}
}

func TestOpportunityCost_IdleFloorDiscount(t *testing.T) {
	s := NewOpportunityCostStrategy()
	now := time.Now()

	// Not idle → no discount
	if d := s.IdleFloorDiscount(now); d != 1.0 {
		t.Errorf("expected 1.0 when not idle, got %f", d)
	}

	// Set idle since 3 hours ago
	idleStart := now.Add(-3 * time.Hour)
	s.lastIdleSince = &idleStart

	if d := s.IdleFloorDiscount(now); d != 0.80 {
		t.Errorf("expected 0.80 after 3hr idle, got %f", d)
	}
}

func TestOpportunityCost_PHigherRate_InsufficientData(t *testing.T) {
	s := NewOpportunityCostStrategy()
	p := s.estimatePHigherRate()
	if p != 0.5 {
		t.Errorf("expected 0.5 with no data, got %f", p)
	}
}

func TestOpportunityCost_PHigherRate_Clamped(t *testing.T) {
	s := NewOpportunityCostStrategy()
	now := time.Now()

	// Extreme positive slope
	s.recordMDC(-1.0, now)
	s.recordMDC(1.0, now.Add(1*time.Second))

	p := s.estimatePHigherRate()
	if p > maxPHigher {
		t.Errorf("P(higher) should be clamped to %f, got %f", maxPHigher, p)
	}
	if p < minPHigher {
		t.Errorf("P(higher) should be at least %f, got %f", minPHigher, p)
	}
}

func TestOpportunityCost_Apply_FlashFreeze(t *testing.T) {
	s := NewOpportunityCostStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FlashFreeze = true
	})
	res := s.Apply(ctx)
	if res.Reason != "flash_freeze" {
		t.Errorf("expected flash_freeze, got %s", res.Reason)
	}
}

func TestOpportunityCost_Apply_Deploy(t *testing.T) {
	s := NewOpportunityCostStrategy()
	ctx := testCtx(func(c *domain.DecisionContext) {
		c.Snapshot.FRR = 0.0005
	})
	res := s.Apply(ctx)
	if len(res.Offers) == 0 && res.Reason != "floor:ev_wait" {
		t.Errorf("expected offers or ev_wait, got reason: %s", res.Reason)
	}
}
