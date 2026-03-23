## 1. Domain Types

- [x] 1.1 Add `SignalHealthState` type (Healthy/Warning/Degraded/Recovering) to `domain/signal.go`
- [x] 1.2 Add `SignalHealthSummary` struct (map of SignalType → SignalHealthState) to `domain/signal.go`
- [x] 1.3 Add `SignalHealth` field to `domain/snapshot.go` MarketSnapshot
- [x] 1.4 Add `DegradedMode` + `DegradedReason` fields to `domain/snapshot.go` MarketSnapshot

## 2. Signal Health Tracker

- [x] 2.1 Create `signal/health.go` — `SignalHealthTracker` struct with per-signal last-update timestamps
- [x] 2.2 Implement `Update(signals []SignalValue, now time.Time)` — record last-update for each signal
- [x] 2.3 Implement `GetHealth(now time.Time, heartbeat time.Duration) SignalHealthSummary` — compute 3-state + recovering
- [x] 2.4 Implement recovery confirmation logic — track consecutive healthy heartbeats, require 2 to reintegrate
- [x] 2.5 Write tests: healthy/warning/degraded transitions, recovery confirmation pass/fail, multiple signals

## 3. MDC Degradation

- [x] 3.1 Extend `MDCAggregator.Aggregate` to accept optional health map parameter
- [x] 3.2 Implement degraded signal exclusion + proportional weight renormalization
- [x] 3.3 Implement liquidation failure fallback (BookConsumption weight → 35%)
- [x] 3.4 Implement Order Book failure → force MDC=0 (FRR-only mode)
- [x] 3.5 Implement momentum failure → redistribute to MarginUsage + CrossCurrency
- [x] 3.6 Ensure nil/empty health map = backward compatible (no behavior change)
- [x] 3.7 Write tests: single degraded, multiple degraded, liquidation fallback, order book fallback, backward compat

## 4. MarketFeed Integration

- [x] 4.1 Add `SignalHealthTracker` to `marketfeed/Service` struct
- [x] 4.2 Call `tracker.Update(signals, now)` after signal computation in `buildSnapshot`
- [x] 4.3 Pass health summary to `mdcAgg.Aggregate` call
- [x] 4.4 Set `DegradedMode` + `DegradedReason` on snapshot when applicable
- [x] 4.5 Include `SignalHealth` summary in assembled snapshot
- [x] 4.6 Write tests: health tracker wired in, degraded mode propagated to snapshot

## 5. Verification

- [x] 5.1 Run full test suite (`go test ./... -race`) — all existing tests pass
- [x] 5.2 Run golangci-lint — no new warnings
- [x] 5.3 Update ROADMAP.md — mark G9 complete
