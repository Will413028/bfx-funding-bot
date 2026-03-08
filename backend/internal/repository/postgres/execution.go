package postgres

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

type ExecutionRepo struct {
	q *sqlc.Queries
}

func NewExecutionRepo(q *sqlc.Queries) *ExecutionRepo {
	return &ExecutionRepo{q: q}
}

func (r *ExecutionRepo) Create(ctx context.Context, record *domain.ExecutionRecord) (*domain.ExecutionRecord, error) {
	uid, err := parseUUID(record.UserID)
	if err != nil {
		return nil, err
	}

	params := sqlc.CreateExecutionParams{
		UserID:   uid,
		Action:   record.Action,
		Currency: record.Currency,
		Amount:   record.Amount,
		Rate:     record.Rate,
		Period:   int32(record.Period),
		Status:   record.Status,
	}

	if record.OfferID != nil {
		params.OfferID = pgtype.Int8{Int64: *record.OfferID, Valid: true}
	}
	if record.ErrorMessage != nil {
		params.ErrorMessage = pgtype.Text{String: *record.ErrorMessage, Valid: true}
	}

	row, err := r.q.CreateExecution(ctx, params)
	if err != nil {
		return nil, err
	}
	return toDomainExecution(row), nil
}

func (r *ExecutionRepo) ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.ExecutionRecord, error) {
	uid, err := parseUUID(userID)
	if err != nil {
		return nil, err
	}

	rows, err := r.q.ListExecutionsByUser(ctx, sqlc.ListExecutionsByUserParams{
		UserID:    uid,
		CreatedAt: pgtype.Timestamptz{Time: since, Valid: true},
		Limit:     int32(limit),
	})
	if err != nil {
		return nil, err
	}

	records := make([]domain.ExecutionRecord, len(rows))
	for i, row := range rows {
		records[i] = *toDomainExecution(row)
	}
	return records, nil
}

func toDomainExecution(row sqlc.Execution) *domain.ExecutionRecord {
	rec := &domain.ExecutionRecord{
		ID:        uuidToString(row.ID),
		UserID:    uuidToString(row.UserID),
		Action:    row.Action,
		Currency:  row.Currency,
		Amount:    row.Amount,
		Rate:      row.Rate,
		Period:    int(row.Period),
		Status:    row.Status,
		CreatedAt: row.CreatedAt.Time,
	}
	if row.OfferID.Valid {
		v := row.OfferID.Int64
		rec.OfferID = &v
	}
	if row.ErrorMessage.Valid {
		v := row.ErrorMessage.String
		rec.ErrorMessage = &v
	}
	return rec
}
