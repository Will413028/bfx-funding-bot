package handler

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"golang.org/x/crypto/bcrypt"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type testUserRepo struct {
	users   map[string]*domain.User
	byEmail map[string]*domain.User
}

func newTestUserRepo() *testUserRepo {
	return &testUserRepo{
		users:   make(map[string]*domain.User),
		byEmail: make(map[string]*domain.User),
	}
}

func (m *testUserRepo) Create(_ context.Context, email, passwordHash string) (*domain.User, error) {
	if _, exists := m.byEmail[email]; exists {
		return nil, &pgconn.PgError{Code: "23505"}
	}
	u := &domain.User{
		ID:           "user-123",
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

func (m *testUserRepo) GetByID(_ context.Context, id string) (*domain.User, error) {
	u, ok := m.users[id]
	if !ok {
		return nil, pgx.ErrNoRows
	}
	return u, nil
}

func (m *testUserRepo) GetByEmail(_ context.Context, email string) (*domain.User, error) {
	u, ok := m.byEmail[email]
	if !ok {
		return nil, pgx.ErrNoRows
	}
	return u, nil
}

func (m *testUserRepo) UpdatePassword(_ context.Context, id, passwordHash string) error {
	u, ok := m.users[id]
	if !ok {
		return pgx.ErrNoRows
	}
	u.PasswordHash = passwordHash
	return nil
}

func (m *testUserRepo) UpdateStatus(_ context.Context, id string, status domain.UserStatus) error {
	u, ok := m.users[id]
	if !ok {
		return pgx.ErrNoRows
	}
	u.Status = status
	return nil
}

// testTokenRepo is a no-op token repo for handler tests.
type testTokenRepo struct{}

func (m *testTokenRepo) Store(_ context.Context, _, _, _ string, _ time.Duration) error { return nil }
func (m *testTokenRepo) Get(_ context.Context, _, _ string) (string, error)             { return "", nil }
func (m *testTokenRepo) Delete(_ context.Context, _, _ string) error                    { return nil }

// testNotifier is a no-op notifier for handler tests.
type testNotifier struct{}

func (m *testNotifier) SendWelcome(_ context.Context, _ string) error         { return nil }
func (m *testNotifier) SendVerification(_ context.Context, _, _ string) error { return nil }
func (m *testNotifier) SendPasswordReset(_ context.Context, _, _ string) error { return nil }
func (m *testNotifier) SendAPIKeyAlert(_ context.Context, _, _ string) error  { return nil }
func (m *testNotifier) SendAlert(_ context.Context, _, _, _ string) error     { return nil }

func setupUserTest(t *testing.T) (*gin.Engine, *testUserRepo) {
	t.Helper()
	repo := newTestUserRepo()
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	jwtMgr := auth.NewJWTManager(priv, &priv.PublicKey)
	userSvc := service.NewUserService(repo, &testTokenRepo{}, jwtMgr, &testNotifier{})
	h := NewUserHandler(userSvc)

	r := gin.New()
	group := r.Group("", func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "user-123")
		c.Set(middleware.ContextEmail, "test@example.com")
		c.Next()
	})
	group.GET("/me", h.GetProfile)
	group.PUT("/me/password", h.ChangePassword)

	// Seed a user
	hash, _ := bcrypt.GenerateFromPassword([]byte("password123"), bcrypt.MinCost)
	repo.users["user-123"] = &domain.User{
		ID:           "user-123",
		Email:        "test@example.com",
		PasswordHash: string(hash),
		Status:       domain.UserStatusActive,
		CreatedAt:    time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		UpdatedAt:    time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
	}

	return r, repo
}

func TestUserHandler_GetProfile(t *testing.T) {
	r, _ := setupUserTest(t)

	req := httptest.NewRequest(http.MethodGet, "/me", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	var envelope map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &envelope)
	body := envelope["data"].(map[string]any)

	if body["email"] != "test@example.com" {
		t.Errorf("expected email test@example.com, got %v", body["email"])
	}
	if body["id"] != "user-123" {
		t.Errorf("expected id user-123, got %v", body["id"])
	}
	if _, exists := body["passwordHash"]; exists {
		t.Error("passwordHash should not be in response")
	}
}

func TestUserHandler_ChangePassword_Success(t *testing.T) {
	r, repo := setupUserTest(t)

	body, _ := json.Marshal(map[string]string{
		"currentPassword": "password123",
		"newPassword":     "newpassword456",
	})
	req := httptest.NewRequest(http.MethodPut, "/me/password", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	// Verify password was updated
	user := repo.users["user-123"]
	if bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte("newpassword456")) != nil {
		t.Error("password was not updated correctly")
	}
}

func TestUserHandler_ChangePassword_WrongCurrent(t *testing.T) {
	r, _ := setupUserTest(t)

	body, _ := json.Marshal(map[string]string{
		"currentPassword": "wrongpassword",
		"newPassword":     "newpassword456",
	})
	req := httptest.NewRequest(http.MethodPut, "/me/password", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d: %s", w.Code, w.Body.String())
	}
}

func TestUserHandler_ChangePassword_TooShort(t *testing.T) {
	r, _ := setupUserTest(t)

	body, _ := json.Marshal(map[string]string{
		"currentPassword": "password123",
		"newPassword":     "short",
	})
	req := httptest.NewRequest(http.MethodPut, "/me/password", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", w.Code, w.Body.String())
	}
}
