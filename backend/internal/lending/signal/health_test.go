package signal

import (
	"testing"
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const testHeartbeat = 3 * time.Second

func allSignalTypes() []domain.SignalType {
	return []domain.SignalType{
		domain.SignalBookConsumption,
		domain.SignalLiquidationCascade,
		domain.SignalMarginUsage,
		domain.SignalMomentum,
		domain.SignalCrossCurrency,
		domain.SignalIntraday,
	}
}

func TestHealth_InitialState_Degraded(t *testing.T) {
	tracker := NewSignalHealthTracker(allSignalTypes())
	now := time.Now()

	health := tracker.GetHealth(now, testHeartbeat)
	for _, sigType := range allSignalTypes() {
		if health[sigType] != domain.SignalDegraded {
			t.Errorf("%s: expected degraded (never updated), got %s", sigType, health[sigType])
		}
	}
}

func TestHealth_Healthy_AfterUpdate(t *testing.T) {
	tracker := NewSignalHealthTracker(allSignalTypes())
	now := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: now},
		{Type: domain.SignalMomentum, Value: 0.3, Confidence: 0.9, Timestamp: now},
	}
	tracker.Update(signals, now)

	health := tracker.GetHealth(now, testHeartbeat)
	if health[domain.SignalBookConsumption] != domain.SignalHealthy {
		t.Errorf("BookConsumption: expected healthy, got %s", health[domain.SignalBookConsumption])
	}
	if health[domain.SignalMomentum] != domain.SignalHealthy {
		t.Errorf("Momentum: expected healthy, got %s", health[domain.SignalMomentum])
	}
	// Not updated signals should still be degraded
	if health[domain.SignalCrossCurrency] != domain.SignalDegraded {
		t.Errorf("CrossCurrency: expected degraded, got %s", health[domain.SignalCrossCurrency])
	}
}

func TestHealth_Warning_AfterStale(t *testing.T) {
	tracker := NewSignalHealthTracker([]domain.SignalType{domain.SignalBookConsumption})
	t0 := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: t0},
	}
	tracker.Update(signals, t0)

	// 3 heartbeats later → warning (>2)
	t1 := t0.Add(3 * testHeartbeat)
	health := tracker.GetHealth(t1, testHeartbeat)
	if health[domain.SignalBookConsumption] != domain.SignalWarning {
		t.Errorf("expected warning after 3 heartbeats, got %s", health[domain.SignalBookConsumption])
	}
}

func TestHealth_Degraded_AfterLongStale(t *testing.T) {
	tracker := NewSignalHealthTracker([]domain.SignalType{domain.SignalBookConsumption})
	t0 := time.Now()

	signals := []domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: t0},
	}
	tracker.Update(signals, t0)

	// 6 heartbeats later → degraded (>5)
	t1 := t0.Add(6 * testHeartbeat)
	health := tracker.GetHealth(t1, testHeartbeat)
	if health[domain.SignalBookConsumption] != domain.SignalDegraded {
		t.Errorf("expected degraded after 6 heartbeats, got %s", health[domain.SignalBookConsumption])
	}
}

func TestHealth_Recovery_RequiresTwoHeartbeats(t *testing.T) {
	tracker := NewSignalHealthTracker([]domain.SignalType{domain.SignalMomentum})
	t0 := time.Now()

	// Initial update
	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalMomentum, Value: 0.5, Confidence: 1.0, Timestamp: t0},
	}, t0)

	// Go degraded (6 heartbeats)
	t1 := t0.Add(6 * testHeartbeat)
	health := tracker.GetHealth(t1, testHeartbeat)
	if health[domain.SignalMomentum] != domain.SignalDegraded {
		t.Fatalf("expected degraded, got %s", health[domain.SignalMomentum])
	}

	// Signal comes back — first heartbeat
	t2 := t1.Add(1 * time.Millisecond)
	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalMomentum, Value: 0.3, Confidence: 0.8, Timestamp: t2},
	}, t2)
	health = tracker.GetHealth(t2, testHeartbeat)
	if health[domain.SignalMomentum] != domain.SignalRecovering {
		t.Errorf("1st heartbeat: expected recovering, got %s", health[domain.SignalMomentum])
	}

	// Second heartbeat — still fresh
	t3 := t2.Add(testHeartbeat)
	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalMomentum, Value: 0.4, Confidence: 0.9, Timestamp: t3},
	}, t3)
	health = tracker.GetHealth(t3, testHeartbeat)
	if health[domain.SignalMomentum] != domain.SignalHealthy {
		t.Errorf("2nd heartbeat: expected healthy (recovery confirmed), got %s", health[domain.SignalMomentum])
	}
}

func TestHealth_Recovery_ResetOnStale(t *testing.T) {
	tracker := NewSignalHealthTracker([]domain.SignalType{domain.SignalMomentum})
	t0 := time.Now()

	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalMomentum, Value: 0.5, Confidence: 1.0, Timestamp: t0},
	}, t0)

	// Go degraded
	t1 := t0.Add(6 * testHeartbeat)
	tracker.GetHealth(t1, testHeartbeat) // trigger degraded

	// Signal comes back — first heartbeat
	t2 := t1.Add(1 * time.Millisecond)
	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalMomentum, Value: 0.3, Confidence: 0.8, Timestamp: t2},
	}, t2)
	health := tracker.GetHealth(t2, testHeartbeat)
	if health[domain.SignalMomentum] != domain.SignalRecovering {
		t.Fatalf("expected recovering, got %s", health[domain.SignalMomentum])
	}

	// Goes stale again during recovery (3 heartbeats) → warning, recovery reset
	t3 := t2.Add(3 * testHeartbeat)
	health = tracker.GetHealth(t3, testHeartbeat)
	if health[domain.SignalMomentum] != domain.SignalWarning {
		t.Errorf("stale during recovery: expected warning, got %s", health[domain.SignalMomentum])
	}

	// Now comes back — needs 2 fresh heartbeats again from scratch
	t4 := t3.Add(1 * time.Millisecond)
	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalMomentum, Value: 0.2, Confidence: 0.7, Timestamp: t4},
	}, t4)
	health = tracker.GetHealth(t4, testHeartbeat)
	if health[domain.SignalMomentum] != domain.SignalRecovering {
		t.Errorf("after reset: expected recovering, got %s", health[domain.SignalMomentum])
	}
}

func TestHealth_MultipleSignals_Independent(t *testing.T) {
	tracker := NewSignalHealthTracker([]domain.SignalType{
		domain.SignalBookConsumption,
		domain.SignalCrossCurrency,
	})
	t0 := time.Now()

	// Update both
	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.5, Confidence: 1.0, Timestamp: t0},
		{Type: domain.SignalCrossCurrency, Value: 0.2, Confidence: 0.8, Timestamp: t0},
	}, t0)

	// 4 heartbeats: book=warning, cross=warning
	t1 := t0.Add(4 * testHeartbeat)
	health := tracker.GetHealth(t1, testHeartbeat)
	if health[domain.SignalBookConsumption] != domain.SignalWarning {
		t.Errorf("Book: expected warning, got %s", health[domain.SignalBookConsumption])
	}

	// Update only book
	tracker.Update([]domain.SignalValue{
		{Type: domain.SignalBookConsumption, Value: 0.6, Confidence: 1.0, Timestamp: t1},
	}, t1)

	health = tracker.GetHealth(t1, testHeartbeat)
	if health[domain.SignalBookConsumption] != domain.SignalHealthy {
		t.Errorf("Book after update: expected healthy, got %s", health[domain.SignalBookConsumption])
	}
	if health[domain.SignalCrossCurrency] != domain.SignalWarning {
		t.Errorf("Cross still stale: expected warning, got %s", health[domain.SignalCrossCurrency])
	}
}
