## ADDED Requirements

### Requirement: FlashCrashDetector type
`lending/marketfeed/flashcrash.go` SHALL define a FlashCrashDetector that monitors market data for extreme conditions.

#### Scenario: Normal market
- **WHEN** the FRR daily change percentage is within normal bounds (default: > -30%)
- **THEN** Check SHALL return FlashFreeze=false

#### Scenario: Flash crash detected
- **WHEN** the FRR daily change percentage drops below the threshold (default: <= -30%)
- **THEN** Check SHALL return FlashFreeze=true

### Requirement: Cooldown period
FlashCrashDetector SHALL enforce a cooldown after a flash crash.

#### Scenario: Cooldown active
- **WHEN** a flash crash was detected and the cooldown period (default 5 minutes) has not elapsed
- **THEN** Check SHALL continue returning FlashFreeze=true even if indicators normalize

#### Scenario: Cooldown expired
- **WHEN** the cooldown period has elapsed and indicators are normal
- **THEN** Check SHALL return FlashFreeze=false

### Requirement: Configurable thresholds
FlashCrashDetector SHALL accept configurable parameters.

#### Scenario: Custom thresholds
- **WHEN** FlashCrashDetector is created with custom rate drop threshold and cooldown duration
- **THEN** it SHALL use those values instead of defaults
