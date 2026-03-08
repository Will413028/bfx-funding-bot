## ADDED Requirements

### Requirement: Book consumption signal
BookConsumption signal SHALL measure order book depletion speed by tracking total depth changes over time.

#### Scenario: Rapid consumption
- **WHEN** book total depth decreases significantly between snapshots
- **THEN** the signal SHALL output a positive value (demand pressure)

#### Scenario: Depth increase
- **WHEN** book total depth increases between snapshots
- **THEN** the signal SHALL output a negative value (supply pressure)

#### Scenario: Cold start
- **WHEN** fewer than 2 data points are available
- **THEN** the signal SHALL output Value=0 with Confidence=0

### Requirement: Liquidation cascade signal
Liquidation signal SHALL detect cascading liquidation events from trade data.

#### Scenario: Large liquidation volume
- **WHEN** accumulated large-volume trades (amount > threshold) in a 5-minute window exceed the liquidation threshold
- **THEN** the signal SHALL output Value=1.0 with Confidence=1.0

#### Scenario: No liquidation
- **WHEN** no significant liquidation activity is detected
- **THEN** the signal SHALL output Value=0 with Confidence=0.5

#### Scenario: Gradual regression
- **WHEN** liquidation was previously triggered but no new large trades arrive for the regression period
- **THEN** the signal SHALL fade from 1.0 → 0.7 → 0.3 → 0.0 over successive intervals

### Requirement: Momentum signal (Dual VWAP)
Momentum signal SHALL compute the difference between fast and slow volume-weighted average rates.

#### Scenario: Bullish momentum
- **WHEN** the fast VWAP (5min) exceeds the slow VWAP (20min)
- **THEN** the signal SHALL output a positive value

#### Scenario: Bearish momentum
- **WHEN** the fast VWAP is below the slow VWAP
- **THEN** the signal SHALL output a negative value

#### Scenario: Insufficient trades
- **WHEN** there are no recent trades to compute VWAP
- **THEN** the signal SHALL output Value=0 with Confidence=0

### Requirement: Margin usage signal
Margin signal SHALL estimate demand pressure from order book bid/ask ratio changes.

#### Scenario: Bid-heavy book
- **WHEN** bid depth exceeds ask depth significantly
- **THEN** the signal SHALL output a positive value (more borrowing demand)

#### Scenario: Ask-heavy book
- **WHEN** ask depth exceeds bid depth significantly
- **THEN** the signal SHALL output a negative value (more lending supply)

### Requirement: Cross-currency signal
CrossCurrency signal SHALL detect funding rate divergence from the FRR trend.

#### Scenario: Rate increasing
- **WHEN** recent trade rates show upward trend vs historical average
- **THEN** the signal SHALL output a positive value

#### Scenario: Rate decreasing
- **WHEN** recent trade rates show downward trend
- **THEN** the signal SHALL output a negative value

### Requirement: All signals implement SignalSource
All 5 signal modules SHALL implement the `marketfeed.SignalSource` interface.

#### Scenario: Interface compliance
- **WHEN** any signal module is used as a SignalSource
- **THEN** it SHALL correctly implement Name() returning the signal type and Compute() returning a valid SignalValue
