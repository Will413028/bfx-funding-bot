package service

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"golang.org/x/crypto/bcrypt"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// mockUserRepo implements repository.UserRepository for testing.
type mockUserRepo struct {
	users  map[string]*domain.User
	byEmail map[string]*domain.User
	createErr error
}

func newMockRepo() *mockUserRepo {
	return &mockUserRepo{
		users:   make(map[string]*domain.User),
		byEmail: make(map[string]*domain.User),
	}
}

func (m *mockUserRepo) Create(_ context.Context, email, passwordHash string) (*domain.User, error) {
	if m.createErr != nil {
		return nil, m.createErr
	}
	if _, exists := m.byEmail[email]; exists {
		return nil, &pgconn.PgError{Code: "23505"}
	}
	u := &domain.User{
		ID:           "test-uuid",
		Email:        email,
		PasswordHash: passwordHash,
		Status:       domain.UserStatusActive,
		CreatedAt:    time.Now(),
		UpdatedAt:    time.Now(),
	}
	m.users[u.ID] = u
	m.byEmail[email] = u
	return u, nil
}

func (m *mockUserRepo) GetByID(_ context.Context, id string) (*domain.User, error) {
	u, ok := m.users[id]
	if !ok {
		return nil, pgx.ErrNoRows
	}
	return u, nil
}

func (m *mockUserRepo) GetByEmail(_ context.Context, email string) (*domain.User, error) {
	u, ok := m.byEmail[email]
	if !ok {
		return nil, pgx.ErrNoRows
	}
	return u, nil
}

func (m *mockUserRepo) UpdatePassword(_ context.Context, id, passwordHash string) error {
	u, ok := m.users[id]
	if !ok {
		return pgx.ErrNoRows
	}
	u.PasswordHash = passwordHash
	return nil
}

func (m *mockUserRepo) UpdateStatus(_ context.Context, id string, status domain.UserStatus) error {
	u, ok := m.users[id]
	if !ok {
		return pgx.ErrNoRows
	}
	u.Status = status
	return nil
}

// mockTokenRepo implements repository.TokenRepository for testing.
type mockTokenRepo struct {
	tokens map[string]string // key = type:hash → userID
}

func newMockTokenRepo() *mockTokenRepo {
	return &mockTokenRepo{tokens: make(map[string]string)}
}

func (m *mockTokenRepo) Store(_ context.Context, tokenHash, userID, tokenType string, _ time.Duration) error {
	m.tokens[tokenType+":"+tokenHash] = userID
	return nil
}

func (m *mockTokenRepo) Get(_ context.Context, tokenHash, tokenType string) (string, error) {
	uid, ok := m.tokens[tokenType+":"+tokenHash]
	if !ok {
		return "", errors.New("not found")
	}
	return uid, nil
}

func (m *mockTokenRepo) Delete(_ context.Context, tokenHash, tokenType string) error {
	delete(m.tokens, tokenType+":"+tokenHash)
	return nil
}

// mockNotifier implements notification.Notifier for testing.
type mockNotifier struct{}

func (m *mockNotifier) SendWelcome(_ context.Context, _ string) error                    { return nil }
func (m *mockNotifier) SendVerification(_ context.Context, _, _ string) error             { return nil }
func (m *mockNotifier) SendPasswordReset(_ context.Context, _, _ string) error            { return nil }
func (m *mockNotifier) SendAPIKeyAlert(_ context.Context, _, _ string) error              { return nil }
func (m *mockNotifier) SendAlert(_ context.Context, _, _, _ string) error                 { return nil }

func testJWTManager(t *testing.T) *auth.JWTManager {
	t.Helper()
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return auth.NewJWTManager(priv, &priv.PublicKey)
}

func TestRegister_Success(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	user, err := svc.Register(context.Background(), "test@example.com", "password123")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if user.Email != "test@example.com" {
		t.Errorf("expected email test@example.com, got %s", user.Email)
	}
}

func TestRegister_InvalidEmail(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, err := svc.Register(context.Background(), "not-an-email", "password123")
	assertAppErrorCode(t, err, "INVALID_EMAIL")
}

func TestRegister_PasswordTooShort(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, err := svc.Register(context.Background(), "test@example.com", "short")
	assertAppErrorCode(t, err, "PASSWORD_TOO_SHORT")
}

func TestRegister_DuplicateEmail(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, _ = svc.Register(context.Background(), "test@example.com", "password123")
	_, err := svc.Register(context.Background(), "test@example.com", "password456")
	assertAppErrorCode(t, err, "EMAIL_ALREADY_EXISTS")
}

func TestLogin_Success(t *testing.T) {
	repo := newMockRepo()
	jwtMgr := testJWTManager(t)
	svc := NewUserService(repo, newMockTokenRepo(), jwtMgr, &mockNotifier{})

	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	result, err := svc.Login(context.Background(), "test@example.com", "password123")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.AccessToken == "" {
		t.Error("expected non-empty access token")
	}
	if result.RefreshToken == "" {
		t.Error("expected non-empty refresh token")
	}
	if time.Until(result.ExpiresAt) < 14*time.Minute {
		t.Error("expected expiry ~15min from now")
	}
}

func TestLogin_WrongPassword(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	_, err := svc.Login(context.Background(), "test@example.com", "wrongpassword")
	assertAppErrorCode(t, err, "INVALID_CREDENTIALS")
}

func TestLogin_NonExistentUser(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, err := svc.Login(context.Background(), "nobody@example.com", "password123")
	assertAppErrorCode(t, err, "INVALID_CREDENTIALS")
}

func TestLogin_SuspendedUser(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	hash, _ := bcrypt.GenerateFromPassword([]byte("password123"), bcrypt.MinCost)
	repo.byEmail["suspended@example.com"] = &domain.User{
		ID:           "sus-uuid",
		Email:        "suspended@example.com",
		PasswordHash: string(hash),
		Status:       domain.UserStatusSuspended,
	}

	_, err := svc.Login(context.Background(), "suspended@example.com", "password123")
	assertAppErrorCode(t, err, "USER_SUSPENDED")
}

func TestGetProfile_Success(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	user, err := svc.GetProfile(context.Background(), "test-uuid")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if user.Email != "test@example.com" {
		t.Errorf("expected email test@example.com, got %s", user.Email)
	}
	if user.PasswordHash != "" {
		t.Error("expected password_hash to be empty in profile response")
	}
}

func TestChangePassword_Success(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	err := svc.ChangePassword(context.Background(), "test-uuid", "password123", "newpassword456")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Verify new password works
	newUser := repo.users["test-uuid"]
	if bcrypt.CompareHashAndPassword([]byte(newUser.PasswordHash), []byte("newpassword456")) != nil {
		t.Error("new password hash does not match")
	}
}

func TestChangePassword_WrongCurrentPassword(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	err := svc.ChangePassword(context.Background(), "test-uuid", "wrongpassword", "newpassword456")
	assertAppErrorCode(t, err, "INVALID_CREDENTIALS")
}

func TestChangePassword_NewPasswordTooShort(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	err := svc.ChangePassword(context.Background(), "test-uuid", "password123", "short")
	assertAppErrorCode(t, err, "PASSWORD_TOO_SHORT")
}

func assertAppErrorCode(t *testing.T, err error, code string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error with code %s, got nil", code)
	}
	var appErr *domain.AppError
	if !errors.As(err, &appErr) {
		t.Fatalf("expected *domain.AppError, got %T: %v", err, err)
	}
	if appErr.Code != code {
		t.Errorf("expected error code %s, got %s", code, appErr.Code)
	}
}
