package service

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/mail"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"golang.org/x/crypto/bcrypt"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/notification"
	"github.com/will/bfx-funding-bot/backend/internal/repository"
)

const (
	bcryptCost             = 12
	tokenBytes             = 32
	verifyTokenTTL         = 24 * time.Hour
	resetTokenTTL          = 1 * time.Hour
	tokenTypeVerify        = "verify"
	tokenTypeReset         = "reset"
)

type UserService struct {
	repo     repository.UserRepository
	tokens   repository.TokenRepository
	jwt      *auth.JWTManager
	notifier notification.Notifier
}

func NewUserService(repo repository.UserRepository, tokens repository.TokenRepository, jwt *auth.JWTManager, notifier notification.Notifier) *UserService {
	return &UserService{repo: repo, tokens: tokens, jwt: jwt, notifier: notifier}
}

func (s *UserService) Register(ctx context.Context, email, password string) (*domain.User, error) {
	if _, err := mail.ParseAddress(email); err != nil {
		return nil, domain.ErrInvalidEmail()
	}
	if len(password) < 8 {
		return nil, domain.ErrPasswordTooShort()
	}
	if len(password) > 72 {
		return nil, domain.ErrPasswordTooLong()
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

	// Generate verification token and send email (best-effort)
	token, tokenHash := generateToken()
	if err := s.tokens.Store(ctx, tokenHash, user.ID, tokenTypeVerify, verifyTokenTTL); err == nil {
		_ = s.notifier.SendVerification(ctx, email, token)
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

	if user.Status == domain.UserStatusPending {
		return "", time.Time{}, domain.ErrEmailNotVerified()
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
	if len(newPassword) > 72 {
		return domain.ErrPasswordTooLong()
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

// VerifyEmail validates a verification token and activates the user.
func (s *UserService) VerifyEmail(ctx context.Context, token string) error {
	tokenHash := hashToken(token)
	userID, err := s.tokens.Get(ctx, tokenHash, tokenTypeVerify)
	if err != nil {
		return domain.ErrInvalidOrExpiredToken()
	}

	if err := s.repo.UpdateStatus(ctx, userID, domain.UserStatusActive); err != nil {
		return domain.ErrInternal("failed to activate user")
	}

	_ = s.tokens.Delete(ctx, tokenHash, tokenTypeVerify)
	return nil
}

// RequestPasswordReset generates a reset token and sends email.
// Always returns nil to prevent email enumeration.
func (s *UserService) RequestPasswordReset(ctx context.Context, email string) error {
	user, err := s.repo.GetByEmail(ctx, email)
	if err != nil {
		return nil // don't reveal whether email exists
	}

	token, tokenHash := generateToken()
	if err := s.tokens.Store(ctx, tokenHash, user.ID, tokenTypeReset, resetTokenTTL); err != nil {
		return nil // best-effort
	}

	_ = s.notifier.SendPasswordReset(ctx, email, token)
	return nil
}

// ResetPassword validates a reset token and sets a new password.
func (s *UserService) ResetPassword(ctx context.Context, token, newPassword string) error {
	if len(newPassword) < 8 {
		return domain.ErrPasswordTooShort()
	}
	if len(newPassword) > 72 {
		return domain.ErrPasswordTooLong()
	}

	tokenHash := hashToken(token)
	userID, err := s.tokens.Get(ctx, tokenHash, tokenTypeReset)
	if err != nil {
		return domain.ErrInvalidOrExpiredToken()
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(newPassword), bcryptCost)
	if err != nil {
		return domain.ErrInternal("failed to hash password")
	}

	if err := s.repo.UpdatePassword(ctx, userID, string(hash)); err != nil {
		return domain.ErrInternal("failed to update password")
	}

	_ = s.tokens.Delete(ctx, tokenHash, tokenTypeReset)
	return nil
}

// generateToken creates a random token and its SHA-256 hash.
// The plaintext token is sent to the user; the hash is stored in Redis.
func generateToken() (plaintext string, hash string) {
	b := make([]byte, tokenBytes)
	_, _ = rand.Read(b)
	plaintext = hex.EncodeToString(b)
	return plaintext, hashToken(plaintext)
}

func hashToken(token string) string {
	h := sha256.Sum256([]byte(token))
	return hex.EncodeToString(h[:])
}
