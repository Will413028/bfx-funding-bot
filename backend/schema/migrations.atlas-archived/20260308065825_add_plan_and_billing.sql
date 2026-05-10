-- Modify "users" table
ALTER TABLE "users" ADD COLUMN "plan" text NOT NULL DEFAULT 'free';
-- Create "billing_records" table
CREATE TABLE "billing_records" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL,
  "period_start" timestamptz NOT NULL,
  "period_end" timestamptz NOT NULL,
  "plan" text NOT NULL DEFAULT 'free',
  "amount" double precision NOT NULL,
  "currency" text NOT NULL DEFAULT 'USD',
  "status" text NOT NULL DEFAULT 'pending',
  "paid_at" timestamptz NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY ("id"),
  CONSTRAINT "fk_billing_user" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON UPDATE NO ACTION ON DELETE CASCADE
);
-- Create index "idx_billing_user_period" to table: "billing_records"
CREATE INDEX "idx_billing_user_period" ON "billing_records" ("user_id", "period_start");
