-- Create "funding_candles" table
CREATE TABLE "funding_candles" (
  "symbol" text NOT NULL,
  "timeframe" text NOT NULL,
  "period_agg" text NOT NULL,
  "mts" bigint NOT NULL,
  "open" double precision NULL,
  "close" double precision NULL,
  "high" double precision NULL,
  "low" double precision NULL,
  "volume" double precision NULL,
  PRIMARY KEY ("symbol", "timeframe", "period_agg", "mts")
);
-- Create index "idx_funding_candles_mts" to table: "funding_candles"
CREATE INDEX "idx_funding_candles_mts" ON "funding_candles" ("mts");
-- Create "funding_stats" table
CREATE TABLE "funding_stats" (
  "symbol" text NOT NULL,
  "mts" bigint NOT NULL,
  "frr" double precision NULL,
  "avg_period" double precision NULL,
  "funding_amount" double precision NULL,
  "funding_amount_used" double precision NULL,
  "funding_below_threshold" double precision NULL,
  PRIMARY KEY ("symbol", "mts")
);
-- Create index "idx_funding_stats_mts" to table: "funding_stats"
CREATE INDEX "idx_funding_stats_mts" ON "funding_stats" ("mts");
