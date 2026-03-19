package postgres

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

type BillingRepo struct {
	q *sqlc.Queries
}

func NewBillingRepo(q *sqlc.Queries) *BillingRepo {
	return &BillingRepo{q: q}
}

func (r *BillingRepo) Create(ctx context.Context, record *domain.BillingRecord) (*domain.BillingRecord, error) {
	uid, err := parseUUID(record.UserID)
	if err != nil {
		return nil, err
	}

	row, err := r.q.CreateBillingRecord(ctx, sqlc.CreateBillingRecordParams{
		UserID:      uid,
		PeriodStart: pgtype.Timestamptz{Time: record.PeriodStart, Valid: true},
		PeriodEnd:   pgtype.Timestamptz{Time: record.PeriodEnd, Valid: true},
		Plan:        record.Plan,
		Amount:      record.Amount,
		Currency:    record.Currency,
		Status:      record.Status,
	})
	if err != nil {
		return nil, err
	}
	return toDomainBilling(row), nil
}

func (r *BillingRepo) ListByUser(ctx context.Context, userID string, since time.Time, limit int) ([]domain.BillingRecord, error) {
	uid, err := parseUUID(userID)
	if err != nil {
		return nil, err
	}

	rows, err := r.q.ListBillingByUser(ctx, sqlc.ListBillingByUserParams{
		UserID:      uid,
		PeriodStart: pgtype.Timestamptz{Time: since, Valid: true},
		Limit:       int32(limit),
	})
	if err != nil {
		return nil, err
	}

	records := make([]domain.BillingRecord, len(rows))
	for i := range rows {
		records[i] = *toDomainBilling(rows[i])
	}
	return records, nil
}

func (r *BillingRepo) ListByUserPaginated(ctx context.Context, userID string, cursorTime *time.Time, cursorID string, limit int) ([]domain.BillingRecord, error) {
	uid, err := parseUUID(userID)
	if err != nil {
		return nil, err
	}

	var rows []sqlc.BillingRecord
	if cursorTime != nil {
		cursorUUID, err := parseUUID(cursorID)
		if err != nil {
			return nil, err
		}
		rows, err = r.q.ListBillingByUserCursor(ctx, sqlc.ListBillingByUserCursorParams{
			UserID:    uid,
			CreatedAt: pgtype.Timestamptz{Time: *cursorTime, Valid: true},
			ID:        cursorUUID,
			Limit:     int32(limit),
		})
		if err != nil {
			return nil, err
		}
	} else {
		rows, err = r.q.ListBillingByUserFirst(ctx, sqlc.ListBillingByUserFirstParams{
			UserID: uid,
			Limit:  int32(limit),
		})
		if err != nil {
			return nil, err
		}
	}

	records := make([]domain.BillingRecord, len(rows))
	for i := range rows {
		records[i] = *toDomainBilling(rows[i])
	}
	return records, nil
}

func toDomainBilling(row sqlc.BillingRecord) *domain.BillingRecord {
	rec := &domain.BillingRecord{
		ID:          uuidToString(row.ID),
		UserID:      uuidToString(row.UserID),
		PeriodStart: row.PeriodStart.Time,
		PeriodEnd:   row.PeriodEnd.Time,
		Plan:        row.Plan,
		Amount:      row.Amount,
		Currency:    row.Currency,
		Status:      row.Status,
		CreatedAt:   row.CreatedAt.Time,
	}
	if row.PaidAt.Valid {
		t := row.PaidAt.Time
		rec.PaidAt = &t
	}
	return rec
}
