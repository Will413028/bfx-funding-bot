package handler

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"golang.org/x/crypto/bcrypt"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

// authTokenRepo is a functional in-memory token repo for auth handler tests.
type authTokenRepo struct {
	tokens map[string]string // "type:hash" → userID
}

func newAuthTokenRepo() *authTokenRepo {
	return &authTokenRepo{tokens: make(map[string]string)}
}

func (m *authTokenRepo) Store(_ context.Context, tokenHash, userID, tokenType string, _ time.Duration) error {
	m.tokens[tokenType+":"+tokenHash] = userID
	return nil
}

func (m *authTokenRepo) Get(_ context.Context, tokenHash, tokenType string) (string, error) {
	uid, ok := m.tokens[tokenType+":"+tokenHash]
	if !ok {
		return "", errors.New("not found")
	}
	return uid, nil
}

func (m *authTokenRepo) Delete(_ context.Context, tokenHash, tokenType string) error {
	delete(m.tokens, tokenType+":"+tokenHash)
	return nil
}

func tokenHash(token string) string {
	h := sha256.Sum256([]byte(token))
	return hex.EncodeToString(h[:])
}

func setupAuthTest(t *testing.T) (*gin.Engine, *testUserRepo, *authTokenRepo) {
	t.Helper()
	gin.SetMode(gin.TestMode)

	repo := newTestUserRepo()
	tokenRepo := newAuthTokenRepo()
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	jwtMgr := auth.NewJWTManager(priv, &priv.PublicKey)
	userSvc := service.NewUserService(repo, tokenRepo, jwtMgr, &testNotifier{})
	h := NewAuthHandler(userSvc)

	r := gin.New()
	authGroup := r.Group("/auth")
	authGroup.POST("/register", h.Register)
	authGroup.POST("/login", h.Login)
	authGroup.POST("/refresh", h.Refresh)
	authGroup.POST("/verify-email", h.VerifyEmail)
	authGroup.POST("/forgot-password", h.ForgotPassword)
	authGroup.POST("/reset-password", h.ResetPassword)

	// Logout requires a protected route with user context.
	protected := r.Group("/auth", func(c *gin.Context) {
		c.Set(middleware.ContextUserID, "user-123")
		c.Next()
	})
	protected.POST("/logout", h.Logout)

	return r, repo, tokenRepo
}

func seedUser(t *testing.T, repo *testUserRepo) {
	t.Helper()
	hash, _ := bcrypt.GenerateFromPassword([]byte("password123"), bcrypt.MinCost)
	u := &domain.User{
		ID:           "user-123",
		Email:        "test@example.com",
		PasswordHash: string(hash),
		Status:       domain.UserStatusActive,
		CreatedAt:    time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		UpdatedAt:    time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
	}
	repo.users[u.ID] = u
	repo.byEmail[u.Email] = u
}

func postJSON(r *gin.Engine, path string, body interface{}) *httptest.ResponseRecorder {
	b, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// ── Register ──

func TestAuthHandler_Register_Success(t *testing.T) {
	r, _, _ := setupAuthTest(t)
	w := postJSON(r, "/auth/register", map[string]string{
		"email":    "new@example.com",
		"password": "password123",
	})
	if w.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d: %s", w.Code, w.Body.String())
	}
	var resp map[string]map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["data"]["email"] != "new@example.com" {
		t.Errorf("expected email new@example.com, got %s", resp["data"]["email"])
	}
}

func TestAuthHandler_Register_ValidationError(t *testing.T) {
	r, _, _ := setupAuthTest(t)
	w := postJSON(r, "/auth/register", map[string]string{})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", w.Code)
	}
}

// ── Login ──

func TestAuthHandler_Login_Success(t *testing.T) {
	r, repo, _ := setupAuthTest(t)
	seedUser(t, repo)

	w := postJSON(r, "/auth/login", map[string]string{
		"email":    "test@example.com",
		"password": "password123",
	})
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp map[string]map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["data"]["accessToken"] == nil || resp["data"]["accessToken"] == "" {
		t.Error("expected non-empty accessToken")
	}
}

func TestAuthHandler_Login_WrongPassword(t *testing.T) {
	r, repo, _ := setupAuthTest(t)
	seedUser(t, repo)

	w := postJSON(r, "/auth/login", map[string]string{
		"email":    "test@example.com",
		"password": "wrongpassword",
	})
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d: %s", w.Code, w.Body.String())
	}
}

// ── Refresh ──

func TestAuthHandler_Refresh_Success(t *testing.T) {
	r, repo, tokenRepo := setupAuthTest(t)
	seedUser(t, repo)

	// Inject a valid refresh token.
	h := tokenHash("valid-refresh")
	_ = tokenRepo.Store(context.Background(), h, "user-123", "refresh", 7*24*time.Hour)

	w := postJSON(r, "/auth/refresh", map[string]string{
		"refreshToken": "valid-refresh",
	})
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
}

func TestAuthHandler_Refresh_InvalidToken(t *testing.T) {
	r, _, _ := setupAuthTest(t)
	w := postJSON(r, "/auth/refresh", map[string]string{
		"refreshToken": "invalid-token",
	})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", w.Code, w.Body.String())
	}
}

// ── Logout ──

func TestAuthHandler_Logout_Success(t *testing.T) {
	r, _, _ := setupAuthTest(t)
	req := httptest.NewRequest(http.MethodPost, "/auth/logout", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
}

// ── VerifyEmail ──

func TestAuthHandler_VerifyEmail_Success(t *testing.T) {
	r, repo, tokenRepo := setupAuthTest(t)
	seedUser(t, repo)

	h := tokenHash("valid-verify")
	_ = tokenRepo.Store(context.Background(), h, "user-123", "verify", 24*time.Hour)

	w := postJSON(r, "/auth/verify-email", map[string]string{
		"token": "valid-verify",
	})
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
}

func TestAuthHandler_VerifyEmail_InvalidToken(t *testing.T) {
	r, _, _ := setupAuthTest(t)
	w := postJSON(r, "/auth/verify-email", map[string]string{
		"token": "bad-token",
	})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", w.Code, w.Body.String())
	}
}

// ── ForgotPassword ──

func TestAuthHandler_ForgotPassword_Always200(t *testing.T) {
	r, _, _ := setupAuthTest(t)

	// Non-existent email should still return 200.
	w := postJSON(r, "/auth/forgot-password", map[string]string{
		"email": "nobody@example.com",
	})
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
}

// ── ResetPassword ──

func TestAuthHandler_ResetPassword_Success(t *testing.T) {
	r, repo, tokenRepo := setupAuthTest(t)
	seedUser(t, repo)

	h := tokenHash("valid-reset")
	_ = tokenRepo.Store(context.Background(), h, "user-123", "reset", time.Hour)

	w := postJSON(r, "/auth/reset-password", map[string]string{
		"token":       "valid-reset",
		"newPassword": "newpassword456",
	})
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
}

func TestAuthHandler_ResetPassword_InvalidToken(t *testing.T) {
	r, _, _ := setupAuthTest(t)
	w := postJSON(r, "/auth/reset-password", map[string]string{
		"token":       "bad-token",
		"newPassword": "newpassword456",
	})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", w.Code, w.Body.String())
	}
}
