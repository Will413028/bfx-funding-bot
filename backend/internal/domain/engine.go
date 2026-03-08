package domain

// EngineStatus represents the current operational state of the lending engine.
type EngineStatus struct {
	Running     bool `json:"running"`
	WorkerCount int  `json:"worker_count"`
}
