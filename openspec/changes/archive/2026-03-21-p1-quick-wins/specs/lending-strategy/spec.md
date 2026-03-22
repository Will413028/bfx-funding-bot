## ADDED Requirements

### Requirement: Auto-renew as safety net

The lending engine SHALL keep auto-renew **enabled** on all active credits as a safety net. When the engine is healthy, it SHALL proactively manage credit expiry by cancelling auto-renew and re-pricing via the composite pipeline before expiry. Auto-renew only activates when the engine fails to act.

#### Scenario: Engine healthy at credit expiry
- **WHEN** a credit is about to expire and the engine is running normally
- **THEN** the engine SHALL cancel auto-renew, run the pricing pipeline, and place a new offer with the computed rate

#### Scenario: Engine down at credit expiry
- **WHEN** a credit expires while the engine is not running
- **THEN** Bitfinex auto-renew SHALL activate automatically, preventing capital idle

### Requirement: Early return risk premium on lockup cost

The lockup cost calculation SHALL include a premium for early return risk (callable bond negative convexity). Longer periods and higher historical early return rates incur higher premiums.

Formula: `earlyReturnPremium = earlyReturnBaseRate × (period / 30.0) × 0.05`

#### Scenario: 30-day period with 30% early return rate
- **WHEN** period is 30 days and earlyReturnBaseRate is 0.3
- **THEN** earlyReturnPremium SHALL be 0.005 (0.3 × 1.0 × 0.05)

#### Scenario: 2-day period — minimal premium
- **WHEN** period is 2 days
- **THEN** earlyReturnPremium SHALL be 0.001 (0.3 × 0.067 × 0.05), negligible

### Requirement: FRR manipulation guard

All strategy calculations using FRR SHALL use an effective FRR that guards against manipulation:
`effectiveFRR = max(FRR, bookMidRate × 0.9)`

#### Scenario: FRR significantly below book mid rate
- **WHEN** FRR is 0.0001 and bookMidRate is 0.0003
- **THEN** effectiveFRR SHALL be 0.00027 (0.0003 × 0.9), not 0.0001

#### Scenario: FRR consistent with book
- **WHEN** FRR is 0.0003 and bookMidRate is 0.0003
- **THEN** effectiveFRR SHALL be 0.0003 (FRR wins, no adjustment)

### Requirement: FRR feedback loop awareness

When the user's managed funding represents a significant fraction of the market, the pricing strategy SHALL reduce FRR's influence to prevent positive feedback loops.

`marketShare ≈ available / orderBook.AskDepth`
When `marketShare > 0.05`: `frrWeight *= 1.0 - (marketShare - 0.05) × 2.0`

#### Scenario: 10% market share
- **WHEN** available is $10,000 and AskDepth is $100,000
- **THEN** frrWeight SHALL be reduced by 10% (marketShare=0.10, reduction = (0.10-0.05)×2 = 0.10)

#### Scenario: 2% market share — no adjustment
- **WHEN** available is $2,000 and AskDepth is $100,000
- **THEN** frrWeight SHALL not be adjusted (marketShare=0.02, below 5% threshold)
