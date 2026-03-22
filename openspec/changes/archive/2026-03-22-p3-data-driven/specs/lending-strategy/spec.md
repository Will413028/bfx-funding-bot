## ADDED Requirements

### Requirement: Auto-renew re-pricing via pipeline

When a credit is about to expire, the system SHALL compute a new rate and period via the composite strategy pipeline instead of renewing at the original parameters.

#### Scenario: Market rate increased since original offer
- **WHEN** a credit at 0.0002 rate expires and current market rate is 0.0004
- **THEN** the renewal offer SHALL use the pipeline-computed rate (~0.0004) instead of 0.0002

#### Scenario: Pipeline failure — fallback to auto-renew
- **WHEN** the pipeline fails to produce valid offers for a renewing credit
- **THEN** the system SHALL fall back to Bitfinex auto-renew (already enabled as safety net)

### Requirement: Order book gap pricing

When the computed offer rate falls within a detected order book gap, the pricing module SHALL adjust to the gap's low end for fastest fill.

#### Scenario: Rate in gap
- **WHEN** bestAsk-relative pricing produces rate 0.00040 and a gap exists [0.00035, 0.00045]
- **THEN** rate SHALL be adjusted to 0.00035 (no competition at this level)

#### Scenario: Rate not in any gap
- **WHEN** computed rate has neighboring offers on both sides
- **THEN** rate SHALL remain unchanged
