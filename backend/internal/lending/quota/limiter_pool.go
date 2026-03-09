package quota

import (
	"sync"

	"golang.org/x/time/rate"
)

// RateLimiterPool manages per-user rate.Limiter instances.
type RateLimiterPool struct {
	mu    sync.Mutex
	users map[string]*rate.Limiter
	rate  rate.Limit
	burst int
}

// NewRateLimiterPool creates a pool with default rate and burst for new user limiters.
func NewRateLimiterPool(ratePerSec float64, burst int) *RateLimiterPool {
	return &RateLimiterPool{
		users: make(map[string]*rate.Limiter),
		rate:  rate.Limit(ratePerSec),
		burst: burst,
	}
}

// Get returns the rate.Limiter for the given user, creating one if it doesn't exist.
func (p *RateLimiterPool) Get(userID string) *rate.Limiter {
	p.mu.Lock()
	defer p.mu.Unlock()

	if l, ok := p.users[userID]; ok {
		return l
	}

	l := rate.NewLimiter(p.rate, p.burst)
	p.users[userID] = l
	return l
}

// Remove deletes the user's limiter from the pool.
func (p *RateLimiterPool) Remove(userID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.users, userID)
}
