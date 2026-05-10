-- Create "executions" table
CREATE TABLE "executions" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL,
  "action" text NOT NULL,
  "currency" text NOT NULL,
  "amount" double precision NOT NULL,
  "rate" double precision NOT NULL,
  "period" integer NOT NULL,
  "offer_id" bigint NULL,
  "status" text NOT NULL DEFAULT 'success',
  "error_message" text NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY ("id"),
  CONSTRAINT "fk_executions_user" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON UPDATE NO ACTION ON DELETE CASCADE
);
-- Create index "idx_executions_user_created" to table: "executions"
CREATE INDEX "idx_executions_user_created" ON "executions" ("user_id", "created_at");
