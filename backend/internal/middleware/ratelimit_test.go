package middleware

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

func newTestRouter() *gin.Engine {
	r := gin.New()
	// High rate, low burst for easy testing
	r.Use(RateLimit(100, 3))
	r.GET("/test", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})
	return r
}

func TestRateLimit_WithinLimit(t *testing.T) {
	r := newTestRouter()

	req := httptest.NewRequest(http.MethodGet, "/test", nil)
	req.RemoteAddr = "1.2.3.4:1234"
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	if v := w.Header().Get("X-RateLimit-Limit"); v != "3" {
		t.Errorf("expected X-RateLimit-Limit=3, got %q", v)
	}
	if v := w.Header().Get("X-RateLimit-Remaining"); v == "" {
		t.Error("expected X-RateLimit-Remaining header to be present")
	}
}

func TestRateLimit_ExceedsBurst(t *testing.T) {
	r := newTestRouter()

	// Exhaust burst of 3
	for i := 0; i < 3; i++ {
		req := httptest.NewRequest(http.MethodGet, "/test", nil)
		req.RemoteAddr = "10.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("request %d: expected 200, got %d", i+1, w.Code)
		}
	}

	// 4th request should be rejected
	req := httptest.NewRequest(http.MethodGet, "/test", nil)
	req.RemoteAddr = "10.0.0.1:1234"
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("expected 429, got %d", w.Code)
	}

	if v := w.Header().Get("Retry-After"); v == "" {
		t.Error("expected Retry-After header")
	}
	if v := w.Header().Get("X-RateLimit-Remaining"); v != "0" {
		t.Errorf("expected X-RateLimit-Remaining=0, got %q", v)
	}

	var body map[string]map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("failed to parse body: %v", err)
	}
	if body["error"]["code"] != "RATE_LIMITED" {
		t.Errorf("expected error code RATE_LIMITED, got %q", body["error"]["code"])
	}
}

func TestRateLimit_IndependentIPs(t *testing.T) {
	r := newTestRouter()

	// Exhaust burst for IP A
	for i := 0; i < 3; i++ {
		req := httptest.NewRequest(http.MethodGet, "/test", nil)
		req.RemoteAddr = "192.168.1.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
	}

	// IP A should be limited
	req := httptest.NewRequest(http.MethodGet, "/test", nil)
	req.RemoteAddr = "192.168.1.1:1234"
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("IP A: expected 429, got %d", w.Code)
	}

	// IP B should still work
	req = httptest.NewRequest(http.MethodGet, "/test", nil)
	req.RemoteAddr = "192.168.1.2:1234"
	w = httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("IP B: expected 200, got %d", w.Code)
	}
}

func TestIPRateLimiter_Cleanup(t *testing.T) {
	rl := &IPRateLimiter{
		rate:  10,
		burst: 5,
	}
	rl.lastCleanup.Store(time.Now().UnixNano())

	// Add entries: one recent, one stale
	recentEntry := &ipEntry{limiter: nil}
	recentEntry.lastSeen.Store(time.Now().UnixNano())
	rl.ips.Store("recent", recentEntry)

	staleEntry := &ipEntry{limiter: nil}
	staleEntry.lastSeen.Store(time.Now().Add(-15 * time.Minute).UnixNano())
	rl.ips.Store("stale", staleEntry)

	rl.cleanup()

	if _, ok := rl.ips.Load("recent"); !ok {
		t.Error("expected recent entry to still exist")
	}
	if _, ok := rl.ips.Load("stale"); ok {
		t.Error("expected stale entry to be removed")
	}
}
