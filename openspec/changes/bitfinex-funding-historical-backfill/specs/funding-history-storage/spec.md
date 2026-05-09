## ADDED Requirements

### Requirement: System SHALL persist Bitfinex funding candles in PostgreSQL

The system SHALL provide a `funding_candles` table that stores OHLCV candle data fetched from Bitfinex's `/v2/candles/trade:{tf}:f{currency}:{period}/hist` endpoint, keyed by `(symbol, timeframe, period_agg, mts)`.

#### Scenario: Inserting a new candle

- **WHEN** a unique `(symbol='fUST', timeframe='1h', period_agg='a30:p2:p30', mts=1778284800000)` row is inserted with OHLCV values
- **THEN** the row is stored and a subsequent `SELECT` returns the same OHLCV values

#### Scenario: Inserting a duplicate candle

- **WHEN** a row with the same composite primary key as an existing candle is inserted using `INSERT ... ON CONFLICT DO NOTHING`
- **THEN** the operation succeeds without error and the existing row remains unchanged

### Requirement: System SHALL persist Bitfinex funding statistics in PostgreSQL

The system SHALL provide a `funding_stats` table that stores FRR, average period, funding amount, and threshold values fetched from Bitfinex's `/v2/funding/stats/f{currency}/hist` endpoint, keyed by `(symbol, mts)`.

#### Scenario: Inserting a new stats record

- **WHEN** a row `(symbol='fUST', mts=1778313900000, frr=5.8e-7, avg_period=22.54, funding_amount=230400898.71, funding_amount_used=127770632.21, funding_below_threshold=219235112.41)` is inserted
- **THEN** the row is stored and a subsequent `SELECT` returns matching values within float precision

#### Scenario: Inserting a duplicate stats record

- **WHEN** a stats row with the same `(symbol, mts)` as an existing record is inserted using `INSERT ... ON CONFLICT DO NOTHING`
- **THEN** the operation succeeds without error and the existing row remains unchanged

### Requirement: System SHALL provide indexed time-range queries

Both `funding_candles` and `funding_stats` SHALL have an index on `mts` to support time-range queries needed by backtest engine.

#### Scenario: Querying candles within a time window

- **WHEN** a query `SELECT ... FROM funding_candles WHERE symbol='fUST' AND timeframe='1h' AND mts BETWEEN <start> AND <end>` is executed
- **THEN** the query plan uses the `mts` index (verifiable via `EXPLAIN`) and returns rows in mts-ordered ascending sequence when `ORDER BY mts ASC` is appended

### Requirement: System SHALL expose typed Go query API via sqlc

The system SHALL provide sqlc-generated Go functions for inserting candles and stats and for querying the latest persisted MTS, allowing downstream code to resume backfill from the correct point without re-fetching existing data.

#### Scenario: Resuming backfill from latest persisted MTS

- **WHEN** code calls `GetEarliestCandleMTS(ctx, symbol='fUST', timeframe='1h', period_agg='a30:p2:p30')` after a partial backfill
- **THEN** the function returns the smallest `mts` value present for that key combination, or a sentinel value (NULL / sql.ErrNoRows) when the table is empty for that key
