package service

import (
	"context"
	"errors"
	"net/mail"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"golang.org/x/crypto/bcrypt"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

const bcryptCost = 12

type UserService struct {
	repo repository.UserRepository
	jwt  *auth.JWTManager
}

func NewUserService(repo repository.UserRepository, jwt *auth.JWTManager) *UserService {
	return &UserService{repo: repo, jwt: jwt}
}

func (s *UserService) Register(ctx context.Context, email, password string) (*domain.User, error) {
	if _, err := mail.ParseAddress(email); err != nil {
		return nil, domain.ErrInvalidEmail()
	}
	if len(password) < 8 {
		return nil, domain.ErrPasswordTooShort()
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcryptCost)
	if err != nil {
		return nil, domain.ErrInternal("failed to hash password")
	}

	user, err := s.repo.Create(ctx, email, string(hash))
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return nil, domain.ErrEmailAlreadyExists()
		}
		return nil, domain.ErrInternal("failed to create user")
	}

	return user, nil
}

func (s *UserService) Login(ctx context.Context, email, password string) (string, time.Time, error) {
	user, err := s.repo.GetByEmail(ctx, email)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", time.Time{}, domain.ErrInvalidCredentials()
		}
		return "", time.Time{}, domain.ErrInternal("failed to query user")
	}

	if user.Status == domain.UserStatusSuspended {
		return "", time.Time{}, domain.ErrUserSuspended()
	}

	if err := bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(password)); err != nil {
		return "", time.Time{}, domain.ErrInvalidCredentials()
	}

	token, expiresAt, err := s.jwt.GenerateToken(user.ID, user.Email)
	if err != nil {
		return "", time.Time{}, domain.ErrInternal("failed to generate token")
	}

	return token, expiresAt, nil
}

func (s *UserService) GetProfile(ctx context.Context, userID string) (*domain.User, error) {
	user, err := s.repo.GetByID(ctx, userID)
	if err != nil {
		return nil, domain.ErrInternal("failed to get user profile")
	}
	user.PasswordHash = ""
	return user, nil
}

func (s *UserService) ChangePassword(ctx context.Context, userID, currentPassword, newPassword string) error {
	if len(newPassword) < 8 {
		return domain.ErrPasswordTooShort()
	}

	user, err := s.repo.GetByID(ctx, userID)
	if err != nil {
		return domain.ErrInternal("failed to get user")
	}

	if err := bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(currentPassword)); err != nil {
		return domain.ErrInvalidCredentials()
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(newPassword), bcryptCost)
	if err != nil {
		return domain.ErrInternal("failed to hash password")
	}

	if err := s.repo.UpdatePassword(ctx, userID, string(hash)); err != nil {
		return domain.ErrInternal("failed to update password")
	}

	return nil
}
