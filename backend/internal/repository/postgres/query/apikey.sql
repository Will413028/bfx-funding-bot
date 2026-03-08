-- name: CreateAPIKey :one
INSERT INTO api_keys (user_id, label, api_key, api_secret)
VALUES ($1, $2, $3, $4)
RETURNING id, user_id, label, api_key, api_secret, created_at, updated_at;

-- name: GetAPIKeyByID :one
SELECT id, user_id, label, api_key, api_secret, created_at, updated_at
FROM api_keys
WHERE id = $1;

-- name: GetAPIKeyByUserID :one
SELECT id, user_id, label, api_key, api_secret, created_at, updated_at
FROM api_keys
WHERE user_id = $1;

-- name: DeleteAPIKey :execrows
DELETE FROM api_keys
WHERE id = $1 AND user_id = $2;
