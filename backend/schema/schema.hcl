schema "public" {}

table "users" {
  schema = schema.public

  column "id" {
    type    = uuid
    default = sql("gen_random_uuid()")
  }
  column "email" {
    type = text
    null = false
  }
  column "password_hash" {
    type = text
    null = false
  }
  column "status" {
    type    = text
    null    = false
    default = "active"
  }
  column "plan" {
    type    = text
    null    = false
    default = "free"
  }
  column "created_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }
  column "updated_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }

  primary_key {
    columns = [column.id]
  }

  index "idx_users_email" {
    columns = [column.email]
    unique  = true
  }
}

table "api_keys" {
  schema = schema.public

  column "id" {
    type    = uuid
    default = sql("gen_random_uuid()")
  }
  column "user_id" {
    type = uuid
    null = false
  }
  column "label" {
    type    = text
    null    = false
    default = ""
  }
  column "api_key" {
    type = text
    null = false
  }
  column "api_secret" {
    type = bytea
    null = false
  }
  column "exchange_status" {
    type    = text
    null    = false
    default = "unverified"
  }
  column "created_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }
  column "updated_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }

  primary_key {
    columns = [column.id]
  }

  index "idx_api_keys_user_id" {
    columns = [column.user_id]
    unique  = true
  }

  foreign_key "fk_api_keys_user" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
    on_delete   = CASCADE
  }
}

table "user_configs" {
  schema = schema.public

  column "id" {
    type    = uuid
    default = sql("gen_random_uuid()")
  }
  column "user_id" {
    type = uuid
    null = false
  }
  column "config" {
    type = jsonb
    null = false
  }
  column "created_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }
  column "updated_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }

  primary_key {
    columns = [column.id]
  }

  index "idx_user_configs_user_id" {
    columns = [column.user_id]
    unique  = true
  }

  foreign_key "fk_user_configs_user" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
    on_delete   = CASCADE
  }
}

table "executions" {
  schema = schema.public

  column "id" {
    type    = uuid
    default = sql("gen_random_uuid()")
  }
  column "user_id" {
    type = uuid
    null = false
  }
  column "action" {
    type = text
    null = false
  }
  column "currency" {
    type = text
    null = false
  }
  column "amount" {
    type = double_precision
    null = false
  }
  column "rate" {
    type = double_precision
    null = false
  }
  column "period" {
    type = integer
    null = false
  }
  column "offer_id" {
    type = bigint
    null = true
  }
  column "status" {
    type    = text
    null    = false
    default = "success"
  }
  column "error_message" {
    type = text
    null = true
  }
  column "created_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }

  primary_key {
    columns = [column.id]
  }

  index "idx_executions_user_created" {
    columns = [column.user_id, column.created_at]
  }

  foreign_key "fk_executions_user" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
    on_delete   = CASCADE
  }
}

table "billing_records" {
  schema = schema.public

  column "id" {
    type    = uuid
    default = sql("gen_random_uuid()")
  }
  column "user_id" {
    type = uuid
    null = false
  }
  column "period_start" {
    type = timestamptz
    null = false
  }
  column "period_end" {
    type = timestamptz
    null = false
  }
  column "plan" {
    type    = text
    null    = false
    default = "free"
  }
  column "amount" {
    type = double_precision
    null = false
  }
  column "currency" {
    type    = text
    null    = false
    default = "USD"
  }
  column "status" {
    type    = text
    null    = false
    default = "pending"
  }
  column "paid_at" {
    type = timestamptz
    null = true
  }
  column "created_at" {
    type    = timestamptz
    null    = false
    default = sql("now()")
  }

  primary_key {
    columns = [column.id]
  }

  index "idx_billing_user_period" {
    columns = [column.user_id, column.period_start]
  }

  foreign_key "fk_billing_user" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
    on_delete   = CASCADE
  }
}

table "funding_candles" {
  schema = schema.public

  column "symbol" {
    type = text
    null = false
  }
  column "timeframe" {
    type = text
    null = false
  }
  column "period_agg" {
    type = text
    null = false
  }
  column "mts" {
    type = bigint
    null = false
  }
  column "open" {
    type = double_precision
    null = true
  }
  column "close" {
    type = double_precision
    null = true
  }
  column "high" {
    type = double_precision
    null = true
  }
  column "low" {
    type = double_precision
    null = true
  }
  column "volume" {
    type = double_precision
    null = true
  }

  primary_key {
    columns = [column.symbol, column.timeframe, column.period_agg, column.mts]
  }

  index "idx_funding_candles_mts" {
    columns = [column.mts]
  }
}

table "funding_stats" {
  schema = schema.public

  column "symbol" {
    type = text
    null = false
  }
  column "mts" {
    type = bigint
    null = false
  }
  column "frr" {
    type = double_precision
    null = true
  }
  column "avg_period" {
    type = double_precision
    null = true
  }
  column "funding_amount" {
    type = double_precision
    null = true
  }
  column "funding_amount_used" {
    type = double_precision
    null = true
  }
  column "funding_below_threshold" {
    type = double_precision
    null = true
  }

  primary_key {
    columns = [column.symbol, column.mts]
  }

  index "idx_funding_stats_mts" {
    columns = [column.mts]
  }
}
