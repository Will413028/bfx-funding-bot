package middleware

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"golang.org/x/time/rate"
)

func newUserRateLimitRouter(r rate.Limit, burst int) *gin.Engine {
	gin.SetMode(gin.TestMode)
	router := gin.New()

	// Simulate JWT middleware setting user_id
	router.Use(func(c *gin.Context) {
		if uid := c.GetHeader("X-Test-UserID"); uid != "" {
			c.Set(ContextUserID, uid)
		}
		c.Next()
	})
	router.Use(UserRateLimit(r, burst))
	router.GET("/test", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})
	return router
}

func TestUserRateLimit_WithinLimit(t *testing.T) {
	r := newUserRateLimitRouter(100, 3)

	req := httptest.NewRequest(http.MethodGet, "/test", nil)
	req.Header.Set("X-Test-UserID", "user-1")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	if v := w.Header().Get("X-RateLimit-Limit"); v != "3" {
		t.Errorf("expected X-RateLimit-Limit=3, got %q", v)
	}
}

func TestUserRateLimit_ExceedsBurst(t *testing.T) {
	r := newUserRateLimitRouter(100, 3)

	for i := 0; i < 3; i++ {
		req := httptest.NewRequest(http.MethodGet, "/test", nil)
		req.Header.Set("X-Test-UserID", "user-2")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("request %d: expected 200, got %d", i+1, w.Code)
		}
	}

	// 4th request should be rejected
	req := httptest.NewRequest(http.MethodGet, "/test", nil)
	req.Header.Set("X-Test-UserID", "user-2")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("expected 429, got %d", w.Code)
	}

	var body map[string]map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("failed to parse body: %v", err)
	}
	if body["error"]["code"] != "RATE_LIMITED" {
		t.Errorf("expected error code RATE_LIMITED, got %q", body["error"]["code"])
	}
}

func TestUserRateLimit_IndependentUsers(t *testing.T) {
	r := newUserRateLimitRouter(100, 3)

	// Exhaust burst for user A
	for i := 0; i < 3; i++ {
		req := httptest.NewRequest(http.MethodGet, "/test", nil)
		req.Header.Set("X-Test-UserID", "user-A")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
	}

	// User A should be limited
	req := httptest.NewRequest(http.MethodGet, "/test", nil)
	req.Header.Set("X-Test-UserID", "user-A")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("user A: expected 429, got %d", w.Code)
	}

	// User B should still work
	req = httptest.NewRequest(http.MethodGet, "/test", nil)
	req.Header.Set("X-Test-UserID", "user-B")
	w = httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("user B: expected 200, got %d", w.Code)
	}
}

func TestUserRateLimit_NoUserID(t *testing.T) {
	r := newUserRateLimitRouter(100, 3)

	// No X-Test-UserID header — should pass through
	req := httptest.NewRequest(http.MethodGet, "/test", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 when no user ID, got %d", w.Code)
	}
}

func TestUserRateLimiter_Cleanup(t *testing.T) {
	rl := &UserRateLimiter{
		rate:  10,
		burst: 5,
	}
	rl.lastCleanup.Store(time.Now().UnixNano())

	recentEntry := &userEntry{limiter: nil}
	recentEntry.lastSeen.Store(time.Now().UnixNano())
	rl.users.Store("recent-user", recentEntry)

	staleEntry := &userEntry{limiter: nil}
	staleEntry.lastSeen.Store(time.Now().Add(-15 * time.Minute).UnixNano())
	rl.users.Store("stale-user", staleEntry)

	rl.cleanup()

	if _, ok := rl.users.Load("recent-user"); !ok {
		t.Error("expected recent user to still exist")
	}
	if _, ok := rl.users.Load("stale-user"); ok {
		t.Error("expected stale user to be removed")
	}
}
