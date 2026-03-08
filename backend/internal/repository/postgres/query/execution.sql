-- name: CreateExecution :one
INSERT INTO executions (user_id, action, currency, amount, rate, period, offer_id, status, error_message)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING id, user_id, action, currency, amount, rate, period, offer_id, status, error_message, created_at;

-- name: ListExecutionsByUser :many
SELECT id, user_id, action, currency, amount, rate, period, offer_id, status, error_message, created_at
FROM executions
WHERE user_id = $1 AND created_at >= $2
ORDER BY created_at DESC
LIMIT $3;
