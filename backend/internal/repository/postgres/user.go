package postgres

import (
	"context"
	"encoding/hex"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository/postgres/sqlc"
)

type UserRepo struct {
	q *sqlc.Queries
}

func NewUserRepo(q *sqlc.Queries) *UserRepo {
	return &UserRepo{q: q}
}

func (r *UserRepo) Create(ctx context.Context, email, passwordHash string) (*domain.User, error) {
	row, err := r.q.CreateUser(ctx, sqlc.CreateUserParams{
		Email:        email,
		PasswordHash: passwordHash,
	})
	if err != nil {
		return nil, err
	}
	return toDomainUser(row), nil
}

func (r *UserRepo) GetByID(ctx context.Context, id string) (*domain.User, error) {
	uid, err := parseUUID(id)
	if err != nil {
		return nil, err
	}
	row, err := r.q.GetUserByID(ctx, uid)
	if err != nil {
		return nil, err
	}
	return toDomainUser(row), nil
}

func (r *UserRepo) GetByEmail(ctx context.Context, email string) (*domain.User, error) {
	row, err := r.q.GetUserByEmail(ctx, email)
	if err != nil {
		return nil, err
	}
	return toDomainUser(row), nil
}

func toDomainUser(row sqlc.User) *domain.User {
	return &domain.User{
		ID:           uuidToString(row.ID),
		Email:        row.Email,
		PasswordHash: row.PasswordHash,
		Status:       domain.UserStatus(row.Status),
		CreatedAt:    row.CreatedAt.Time,
		UpdatedAt:    row.UpdatedAt.Time,
	}
}

func uuidToString(u pgtype.UUID) string {
	if !u.Valid {
		return ""
	}
	b := u.Bytes
	dst := make([]byte, 36)
	hex.Encode(dst[0:8], b[0:4])
	dst[8] = '-'
	hex.Encode(dst[9:13], b[4:6])
	dst[13] = '-'
	hex.Encode(dst[14:18], b[6:8])
	dst[18] = '-'
	hex.Encode(dst[19:23], b[8:10])
	dst[23] = '-'
	hex.Encode(dst[24:36], b[10:16])
	return string(dst)
}

func parseUUID(s string) (pgtype.UUID, error) {
	var u pgtype.UUID
	err := u.Scan(s)
	return u, err
}
