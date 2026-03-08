package worker

import "sync/atomic"

// WorkerState represents the lifecycle state of a Worker.
type WorkerState int32

const (
	StateStarting WorkerState = iota
	StateRunning
	StateStopping
	StateStopped
)

// String returns a human-readable state name.
func (s WorkerState) String() string {
	switch s {
	case StateStarting:
		return "starting"
	case StateRunning:
		return "running"
	case StateStopping:
		return "stopping"
	case StateStopped:
		return "stopped"
	default:
		return "unknown"
	}
}

// lifecycle manages worker state transitions atomically.
type lifecycle struct {
	state atomic.Int32
}

func newLifecycle() *lifecycle {
	lc := &lifecycle{}
	lc.state.Store(int32(StateStarting))
	return lc
}

func (lc *lifecycle) State() WorkerState {
	return WorkerState(lc.state.Load())
}

func (lc *lifecycle) transition(to WorkerState) {
	lc.state.Store(int32(to))
}
