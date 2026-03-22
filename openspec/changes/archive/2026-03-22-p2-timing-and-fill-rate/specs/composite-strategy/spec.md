## MODIFIED Requirements

### Requirement: Stage 1 Rate Resolution

Rate resolution SHALL compute the final rate using **bestAsk-relative pricing** as the primary method:
1. `rate = bestAsk - tickOffset(MDC, regime)` — offset decreases when bullish, increases when bearish
2. If bestAsk unavailable, fallback to FRR-based pricing
3. `rate = smartWallPosition(rate, walls)` — price just below nearest wall
4. `rate *= (1.0 + FRRTrend × 0.1)` — trend adjustment
5. `rate *= ComputeQueueDiscount()` — sigmoid curve discount
6. Remaining adjustments unchanged (floor, weekend, calendar, lockup, impact)

#### Scenario: BestAsk available with bullish MDC
- **WHEN** bestAsk is 0.0003 and MDC is +0.8 in contango
- **THEN** rate SHALL be close to bestAsk (small offset) rather than FRR-derived

#### Scenario: BestAsk unavailable
- **WHEN** OrderBook.BestAsk is 0 (empty book)
- **THEN** system SHALL fallback to FRR × MDC premium pricing

#### Scenario: Wall nearby — smart positioning
- **WHEN** a large offer wall exists at 0.0004 and computed rate is 0.00039
- **THEN** rate SHALL be set to `0.0004 - minTickSize` to queue just ahead of wall

## ADDED Requirements

### Requirement: Cascade phase response

The composite pipeline SHALL adjust period based on liquidation cascade phase:
- **Early** (0-30min): `period = cfg.Period.Min` — capture spike with shortest lockup
- **Mid** (30min-2hr): `period = min(14, cfg.Period.Max)` — lock in confirmed high rate
- **Late** (2hr+): skip offer generation — rates declining, wait for next opportunity

#### Scenario: Early cascade — short period
- **WHEN** cascade phase is "early" (triggered 10 minutes ago)
- **THEN** period SHALL be cfg.Period.Min

#### Scenario: Late cascade — no new offers
- **WHEN** cascade phase is "late" (triggered 3 hours ago, rates declining)
- **THEN** system SHALL return empty offers with reason "composite:cascade_late"

### Requirement: Dynamic weekend premium

Weekend premium SHALL use historical weekend/weekday rate ratio instead of fixed percentages.
Formula: `weekendMultiplier = rolling4wkWeekendAvg / rolling4wkWeekdayAvg`
Fallback: fixed premiums (1.02-1.05) when historical data insufficient.

#### Scenario: Historical data available
- **WHEN** 4 weeks of rate data exists and weekend rates average 1.3× weekday rates
- **THEN** weekendMultiplier SHALL be 1.3

#### Scenario: No historical data
- **WHEN** less than 4 weeks of data collected
- **THEN** system SHALL use fixed fallback premiums (Friday 1.02, Saturday 1.05, Sunday 1.03)
