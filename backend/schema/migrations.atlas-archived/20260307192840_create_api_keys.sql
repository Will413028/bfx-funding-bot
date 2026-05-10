-- Create "api_keys" table
CREATE TABLE "api_keys" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL,
  "label" text NOT NULL DEFAULT '',
  "api_key" text NOT NULL,
  "api_secret" bytea NOT NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  "updated_at" timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY ("id"),
  CONSTRAINT "fk_api_keys_user" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON UPDATE NO ACTION ON DELETE CASCADE
);
-- Create index "idx_api_keys_user_id" to table: "api_keys"
CREATE UNIQUE INDEX "idx_api_keys_user_id" ON "api_keys" ("user_id");
