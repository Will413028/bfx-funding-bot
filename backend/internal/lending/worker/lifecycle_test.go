package worker

import "testing"

func TestLifecycle_InitialState(t *testing.T) {
	lc := newLifecycle()
	if lc.State() != StateStarting {
		t.Errorf("initial state: got %s, want starting", lc.State())
	}
}

func TestLifecycle_Transitions(t *testing.T) {
	lc := newLifecycle()

	lc.transition(StateRunning)
	if lc.State() != StateRunning {
		t.Errorf("state: got %s, want running", lc.State())
	}

	lc.transition(StateStopping)
	if lc.State() != StateStopping {
		t.Errorf("state: got %s, want stopping", lc.State())
	}

	lc.transition(StateStopped)
	if lc.State() != StateStopped {
		t.Errorf("state: got %s, want stopped", lc.State())
	}
}

func TestWorkerState_String(t *testing.T) {
	tests := []struct {
		want  string
		state WorkerState
	}{
		{"starting", StateStarting},
		{"running", StateRunning},
		{"stopping", StateStopping},
		{"stopped", StateStopped},
		{"unknown", WorkerState(99)},
	}
	for _, tt := range tests {
		if got := tt.state.String(); got != tt.want {
			t.Errorf("State(%d).String(): got %s, want %s", tt.state, got, tt.want)
		}
	}
}
