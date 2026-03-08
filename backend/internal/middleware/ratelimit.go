package middleware

import (
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"golang.org/x/time/rate"
)

type ipEntry struct {
	limiter  *rate.Limiter
	lastSeen time.Time
}

// IPRateLimiter manages per-IP rate limiters with automatic cleanup.
type IPRateLimiter struct {
	ips   sync.Map
	rate  rate.Limit
	burst int

	stopCleanup chan struct{}
}

// NewIPRateLimiter creates a rate limiter that tracks per-IP request rates.
// Call Stop() to release the background cleanup goroutine.
func NewIPRateLimiter(r rate.Limit, burst int) *IPRateLimiter {
	rl := &IPRateLimiter{
		rate:        r,
		burst:       burst,
		stopCleanup: make(chan struct{}),
	}
	go rl.cleanupLoop()
	return rl
}

func (rl *IPRateLimiter) getLimiter(ip string) *rate.Limiter {
	now := time.Now()

	if v, ok := rl.ips.Load(ip); ok {
		entry := v.(*ipEntry)
		entry.lastSeen = now
		return entry.limiter
	}

	limiter := rate.NewLimiter(rl.rate, rl.burst)
	rl.ips.Store(ip, &ipEntry{limiter: limiter, lastSeen: now})
	return limiter
}

// cleanupLoop removes entries idle for more than 10 minutes, every 5 minutes.
func (rl *IPRateLimiter) cleanupLoop() {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()

	for {
		select {
		case <-rl.stopCleanup:
			return
		case <-ticker.C:
			rl.cleanup()
		}
	}
}

func (rl *IPRateLimiter) cleanup() {
	cutoff := time.Now().Add(-10 * time.Minute)
	rl.ips.Range(func(key, value any) bool {
		entry := value.(*ipEntry)
		if entry.lastSeen.Before(cutoff) {
			rl.ips.Delete(key)
		}
		return true
	})
}

// Stop terminates the background cleanup goroutine.
func (rl *IPRateLimiter) Stop() {
	close(rl.stopCleanup)
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
