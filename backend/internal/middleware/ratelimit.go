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

type ipEntry struct {
	limiter  *rate.Limiter
	lastSeen atomic.Int64 // UnixNano
}

// IPRateLimiter manages per-IP rate limiters with automatic cleanup.
type IPRateLimiter struct {
	ips         sync.Map
	rate        rate.Limit
	burst       int
	lastCleanup atomic.Int64 // UnixNano
}

// NewIPRateLimiter creates a rate limiter that tracks per-IP request rates.
func NewIPRateLimiter(r rate.Limit, burst int) *IPRateLimiter {
	rl := &IPRateLimiter{
		rate:  r,
		burst: burst,
	}
	rl.lastCleanup.Store(time.Now().UnixNano())
	return rl
}

const cleanupInterval = int64(5 * time.Minute)

func (rl *IPRateLimiter) getLimiter(ip string) *rate.Limiter {
	now := time.Now().UnixNano()

	// Lazy cleanup every 5 minutes (CAS prevents duplicate goroutines)
	old := rl.lastCleanup.Load()
	if now-old > cleanupInterval && rl.lastCleanup.CompareAndSwap(old, now) {
		go rl.cleanup()
	}

	newEntry := &ipEntry{limiter: rate.NewLimiter(rl.rate, rl.burst)}
	newEntry.lastSeen.Store(now)

	v, _ := rl.ips.LoadOrStore(ip, newEntry)
	entry := v.(*ipEntry)
	entry.lastSeen.Store(now)
	return entry.limiter
}

func (rl *IPRateLimiter) cleanup() {
	cutoff := time.Now().Add(-10 * time.Minute).UnixNano()
	rl.ips.Range(func(key, value any) bool {
		entry := value.(*ipEntry)
		if entry.lastSeen.Load() < cutoff {
			rl.ips.Delete(key)
		}
		return true
	})
}

// RateLimit returns a Gin middleware that enforces per-IP rate limiting.
func RateLimit(r rate.Limit, burst int) gin.HandlerFunc {
	limiter := NewIPRateLimiter(r, burst)
	limitStr := strconv.Itoa(burst)

	return func(c *gin.Context) {
		ip := c.ClientIP()
		l := limiter.getLimiter(ip)

		if !l.Allow() {
			retryAfter := int(math.Ceil(1.0 / float64(r)))
			c.Header("X-RateLimit-Limit", limitStr)
			c.Header("X-RateLimit-Remaining", "0")
			c.Header("Retry-After", strconv.Itoa(retryAfter))
			c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{
				"error": gin.H{
					"code":    "RATE_LIMITED",
					"message": "Too many requests",
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
