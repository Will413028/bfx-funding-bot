-- name: CreateAPIKey :one
INSERT INTO api_keys (user_id, label, api_key, api_secret, exchange_status)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, user_id, label, api_key, api_secret, created_at, updated_at, exchange_status;

-- name: GetAPIKeyByID :one
SELECT id, user_id, label, api_key, api_secret, created_at, updated_at, exchange_status
FROM api_keys
WHERE id = $1;

-- name: GetAPIKeyByUserID :one
SELECT id, user_id, label, api_key, api_secret, created_at, updated_at, exchange_status
FROM api_keys
WHERE user_id = $1;

-- name: ListVerifiedAPIKeys :many
SELECT id, user_id, label, api_key, api_secret, created_at, updated_at, exchange_status
FROM api_keys
WHERE exchange_status = 'verified';

-- name: UpdateExchangeStatus :exec
UPDATE api_keys
SET exchange_status = $2, updated_at = now()
WHERE id = $1;

-- name: DeleteAPIKey :execrows
DELETE FROM api_keys
WHERE id = $1 AND user_id = $2;
