-- name: UpsertConfig :one
INSERT INTO user_configs (user_id, config)
VALUES ($1, $2)
ON CONFLICT (user_id) DO UPDATE SET
  config = EXCLUDED.config,
  updated_at = now()
RETURNING id, user_id, config, created_at, updated_at;

-- name: GetConfigByUserID :one
SELECT id, user_id, config, created_at, updated_at
FROM user_configs
WHERE user_id = $1;

-- name: DeleteConfigByUserID :execrows
DELETE FROM user_configs
WHERE user_id = $1;
