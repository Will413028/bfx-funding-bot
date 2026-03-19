package middleware

import (
	"math"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"golang.org/x/time/rate"
)

type userEntry struct {
	limiter  *rate.Limiter
	lastSeen atomic.Int64 // UnixNano
}

// UserRateLimiter manages per-user rate limiters with automatic cleanup.
type UserRateLimiter struct {
	users       sync.Map
	rate        rate.Limit
	burst       int
	lastCleanup atomic.Int64 // UnixNano
}

// NewUserRateLimiter creates a rate limiter that tracks per-user request rates.
func NewUserRateLimiter(r rate.Limit, burst int) *UserRateLimiter {
	rl := &UserRateLimiter{
		rate:  r,
		burst: burst,
	}
	rl.lastCleanup.Store(time.Now().UnixNano())
	return rl
}

const userCleanupInterval = int64(5 * time.Minute)

func (rl *UserRateLimiter) getLimiter(userID string) *rate.Limiter {
	now := time.Now().UnixNano()

	old := rl.lastCleanup.Load()
	if now-old > userCleanupInterval && rl.lastCleanup.CompareAndSwap(old, now) {
		go rl.cleanup()
	}

	newEntry := &userEntry{limiter: rate.NewLimiter(rl.rate, rl.burst)}
	newEntry.lastSeen.Store(now)

	v, _ := rl.users.LoadOrStore(userID, newEntry)
	entry := v.(*userEntry)
	entry.lastSeen.Store(now)
	return entry.limiter
}

func (rl *UserRateLimiter) cleanup() {
	cutoff := time.Now().Add(-10 * time.Minute).UnixNano()
	rl.users.Range(func(key, value any) bool {
		entry := value.(*userEntry)
		if entry.lastSeen.Load() < cutoff {
			rl.users.Delete(key)
		}
		return true
	})
}

// UserRateLimit returns a Gin middleware that enforces per-user rate limiting.
// Must be placed AFTER JWTAuth middleware so that ContextUserID is available.
func UserRateLimit(r rate.Limit, burst int) gin.HandlerFunc {
	limiter := NewUserRateLimiter(r, burst)
	limitStr := strconv.Itoa(burst)

	return func(c *gin.Context) {
		userID, exists := c.Get(ContextUserID)
		if !exists {
			// No user ID in context — skip (should not happen after JWTAuth)
			c.Next()
			return
		}

		l := limiter.getLimiter(userID.(string))

		if !l.Allow() {
			retryAfter := int(math.Ceil(1.0 / float64(r)))
			c.Header("X-RateLimit-Limit", limitStr)
			c.Header("X-RateLimit-Remaining", "0")
			c.Header("Retry-After", strconv.Itoa(retryAfter))
			c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{
				"error": gin.H{
					"code":    "RATE_LIMITED",
					"message": "Too many requests for this user",
				},
			})
			return
		}

		remaining := int(l.Tokens())
		if remaining < 0 {
			remaining = 0
		}
		c.Header("X-RateLimit-Limit", limitStr)
		c.Header("X-RateLimit-Remaining", strconv.Itoa(remaining))
		c.Next()
	}
}
