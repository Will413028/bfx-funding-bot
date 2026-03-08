-- name: CreateBillingRecord :one
INSERT INTO billing_records (user_id, period_start, period_end, plan, amount, currency, status)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING id, user_id, period_start, period_end, plan, amount, currency, status, paid_at, created_at;

-- name: ListBillingByUser :many
SELECT id, user_id, period_start, period_end, plan, amount, currency, status, paid_at, created_at
FROM billing_records
WHERE user_id = $1 AND period_start >= $2
ORDER BY period_start DESC
LIMIT $3;
