-- Modify "api_keys" table
ALTER TABLE "api_keys" ADD COLUMN "exchange_status" text NOT NULL DEFAULT 'unverified';
