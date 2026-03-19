package signal

import (
	"time"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	// Health thresholds in heartbeat counts (§6.3)
	warningHeartbeats  = 2
	degradedHeartbeats = 5

	// Recovery confirmation: signal must be healthy for this many heartbeats
	recoveryConfirmationHeartbeats = 2
)

// signalState tracks per-signal timing and recovery state.
type signalState struct {
	lastUpdate         time.Time
	wasDegraded        bool // true if signal was in Degraded state
	recoveryHeartbeats int  // consecutive healthy heartbeats since recovery started
}

// SignalHealthTracker monitors the health of each signal source
// based on data freshness relative to heartbeat intervals.
type SignalHealthTracker struct {
	states map[domain.SignalType]*signalState
}

// NewSignalHealthTracker creates a tracker for the given signal types.
func NewSignalHealthTracker(signalTypes []domain.SignalType) *SignalHealthTracker {
	states := make(map[domain.SignalType]*signalState, len(signalTypes))
	for _, st := range signalTypes {
		states[st] = &signalState{}
	}
	return &SignalHealthTracker{states: states}
}

// Update records the latest timestamps from computed signals.
func (t *SignalHealthTracker) Update(signals []domain.SignalValue, now time.Time) {
	for _, s := range signals {
		st, ok := t.states[s.Type]
		if !ok {
			continue
		}
		// Only update if the signal has a valid timestamp
		if !s.Timestamp.IsZero() {
			st.lastUpdate = now
		}
	}
}

// GetHealth computes the health state of each tracked signal.
func (t *SignalHealthTracker) GetHealth(now time.Time, heartbeat time.Duration) domain.SignalHealthSummary {
	summary := make(domain.SignalHealthSummary, len(t.states))

	for sigType, st := range t.states {
		// Never updated → degraded
		if st.lastUpdate.IsZero() {
			summary[sigType] = domain.SignalDegraded
			continue
		}

		age := now.Sub(st.lastUpdate)
		heartbeatsStale := age / heartbeat

		switch {
		case heartbeatsStale > degradedHeartbeats:
			// Enter degraded state
			st.wasDegraded = true
			st.recoveryHeartbeats = 0
			summary[sigType] = domain.SignalDegraded

		case heartbeatsStale > warningHeartbeats:
			// Warning state — reset recovery if was recovering
			if st.wasDegraded {
				st.recoveryHeartbeats = 0
			}
			summary[sigType] = domain.SignalWarning

		default:
			// Data is fresh — check if recovering from degraded
			if st.wasDegraded {
				st.recoveryHeartbeats++
				if st.recoveryHeartbeats >= recoveryConfirmationHeartbeats {
					// Recovery confirmed — back to healthy
					st.wasDegraded = false
					st.recoveryHeartbeats = 0
					summary[sigType] = domain.SignalHealthy
				} else {
					summary[sigType] = domain.SignalRecovering
				}
			} else {
				summary[sigType] = domain.SignalHealthy
			}
		}
	}

	return summary
}
