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
	users     map[string]*domain.User
	byEmail   map[string]*domain.User
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

func (m *mockNotifier) SendWelcome(_ context.Context, _ string) error          { return nil }
func (m *mockNotifier) SendVerification(_ context.Context, _, _ string) error  { return nil }
func (m *mockNotifier) SendPasswordReset(_ context.Context, _, _ string) error { return nil }
func (m *mockNotifier) SendAPIKeyAlert(_ context.Context, _, _ string) error   { return nil }
func (m *mockNotifier) SendAlert(_ context.Context, _, _, _ string) error      { return nil }

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

// ── VerifyEmail ──

func TestVerifyEmail_Success(t *testing.T) {
	repo := newMockRepo()
	tokenRepo := newMockTokenRepo()
	svc := NewUserService(repo, tokenRepo, testJWTManager(t), &mockNotifier{})

	// Create a pending user and inject a verify token.
	u, _ := svc.Register(context.Background(), "test@example.com", "password123")
	tokenHash := hashToken("valid-verify-token")
	_ = tokenRepo.Store(context.Background(), tokenHash, u.ID, tokenTypeVerify, verifyTokenTTL)

	err := svc.VerifyEmail(context.Background(), "valid-verify-token")
	if err != nil {
		t.Fatalf("expected nil, got %v", err)
	}
	if repo.users[u.ID].Status != domain.UserStatusActive {
		t.Errorf("expected status active, got %s", repo.users[u.ID].Status)
	}
	// Token should be deleted.
	if _, getErr := tokenRepo.Get(context.Background(), tokenHash, tokenTypeVerify); getErr == nil {
		t.Error("expected verify token to be deleted")
	}
}

func TestVerifyEmail_InvalidToken(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})
	err := svc.VerifyEmail(context.Background(), "nonexistent-token")
	assertAppErrorCode(t, err, "INVALID_TOKEN")
}

// ── RequestPasswordReset ──

func TestRequestPasswordReset_ExistingEmail(t *testing.T) {
	repo := newMockRepo()
	svc := NewUserService(repo, newMockTokenRepo(), testJWTManager(t), &mockNotifier{})
	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	err := svc.RequestPasswordReset(context.Background(), "test@example.com")
	if err != nil {
		t.Fatalf("expected nil, got %v", err)
	}
}

func TestRequestPasswordReset_Cooldown(t *testing.T) {
	repo := newMockRepo()
	tokenRepo := newMockTokenRepo()
	svc := NewUserService(repo, tokenRepo, testJWTManager(t), &mockNotifier{})
	_, _ = svc.Register(context.Background(), "test@example.com", "password123")

	// First call should succeed and set cooldown.
	_ = svc.RequestPasswordReset(context.Background(), "test@example.com")

	// Count tokens before second call.
	tokensBefore := len(tokenRepo.tokens)

	// Second call should be silently skipped (cooldown active).
	err := svc.RequestPasswordReset(context.Background(), "test@example.com")
	if err != nil {
		t.Fatalf("expected nil, got %v", err)
	}

	// No new tokens should be created (cooldown blocked it).
	if len(tokenRepo.tokens) != tokensBefore {
		t.Errorf("expected no new tokens during cooldown, got %d new", len(tokenRepo.tokens)-tokensBefore)
	}
}

func TestRequestPasswordReset_NonExistentEmail(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})

	err := svc.RequestPasswordReset(context.Background(), "nobody@example.com")
	if err != nil {
		t.Fatalf("expected nil (prevent enumeration), got %v", err)
	}
}

// ── ResetPassword ──

func TestResetPassword_Success(t *testing.T) {
	repo := newMockRepo()
	tokenRepo := newMockTokenRepo()
	svc := NewUserService(repo, tokenRepo, testJWTManager(t), &mockNotifier{})

	u, _ := svc.Register(context.Background(), "test@example.com", "password123")
	oldHash := repo.users[u.ID].PasswordHash

	tokenHash := hashToken("valid-reset-token")
	_ = tokenRepo.Store(context.Background(), tokenHash, u.ID, tokenTypeReset, resetTokenTTL)

	err := svc.ResetPassword(context.Background(), "valid-reset-token", "newpassword456")
	if err != nil {
		t.Fatalf("expected nil, got %v", err)
	}
	if repo.users[u.ID].PasswordHash == oldHash {
		t.Error("expected password hash to change")
	}
	if _, getErr := tokenRepo.Get(context.Background(), tokenHash, tokenTypeReset); getErr == nil {
		t.Error("expected reset token to be deleted")
	}
}

func TestResetPassword_InvalidToken(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})
	err := svc.ResetPassword(context.Background(), "bad-token", "newpassword456")
	assertAppErrorCode(t, err, "INVALID_TOKEN")
}

func TestResetPassword_PasswordTooShort(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})
	err := svc.ResetPassword(context.Background(), "any-token", "short")
	assertAppErrorCode(t, err, "PASSWORD_TOO_SHORT")
}

func TestResetPassword_PasswordTooLong(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})
	long := string(make([]byte, 73))
	err := svc.ResetPassword(context.Background(), "any-token", long)
	assertAppErrorCode(t, err, "PASSWORD_TOO_LONG")
}

// ── RefreshToken ──

func TestRefreshToken_Success(t *testing.T) {
	repo := newMockRepo()
	tokenRepo := newMockTokenRepo()
	jwtMgr := testJWTManager(t)
	svc := NewUserService(repo, tokenRepo, jwtMgr, &mockNotifier{})

	u, _ := svc.Register(context.Background(), "test@example.com", "password123")

	tokenHash := hashToken("valid-refresh-token")
	_ = tokenRepo.Store(context.Background(), tokenHash, u.ID, tokenTypeRefresh, refreshTokenTTL)

	result, err := svc.RefreshToken(context.Background(), "valid-refresh-token")
	if err != nil {
		t.Fatalf("expected nil, got %v", err)
	}
	if result.AccessToken == "" {
		t.Error("expected non-empty access token")
	}
	if result.RefreshToken == "" {
		t.Error("expected non-empty refresh token")
	}
	if result.ExpiresAt.IsZero() {
		t.Error("expected non-zero expiresAt")
	}
	// Old refresh token should be deleted (rotation).
	if _, getErr := tokenRepo.Get(context.Background(), tokenHash, tokenTypeRefresh); getErr == nil {
		t.Error("expected old refresh token to be deleted")
	}
}

func TestRefreshToken_InvalidToken(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})
	_, err := svc.RefreshToken(context.Background(), "bad-refresh-token")
	assertAppErrorCode(t, err, "INVALID_TOKEN")
}

// ── Logout ──

func TestLogout_Success(t *testing.T) {
	svc := NewUserService(newMockRepo(), newMockTokenRepo(), testJWTManager(t), &mockNotifier{})
	err := svc.Logout(context.Background(), "any-user-id")
	if err != nil {
		t.Fatalf("expected nil, got %v", err)
	}
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
