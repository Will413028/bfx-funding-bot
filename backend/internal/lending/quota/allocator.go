package quota

import (
	"context"
	"sync"
	"time"
)

// userEntry tracks per-user quota state.
type userEntry struct {
	limit int
	used  int
}

// Allocator manages global and per-user API call quotas.
// Thread-safe for concurrent use by multiple Worker goroutines.
type Allocator struct {
	mu         sync.Mutex
	globalMax  int
	globalUsed int
	users      map[string]*userEntry
}

// NewAllocator creates a new Allocator with the given global maximum
// requests per window.
func NewAllocator(globalMax int) *Allocator {
	return &Allocator{
		globalMax: globalMax,
		users:     make(map[string]*userEntry),
	}
}

// Acquire tries to consume n API call tokens. Returns true if both
// global and per-user quotas have enough remaining. Atomic: either
// both are decremented or neither is.
func (a *Allocator) Acquire(userID string, n int) bool {
	a.mu.Lock()
	defer a.mu.Unlock()

	u, exists := a.users[userID]
	if !exists {
		return false
	}

	globalRemaining := a.globalMax - a.globalUsed
	userRemaining := u.limit - u.used

	if globalRemaining < n || userRemaining < n {
		return false
	}

	a.globalUsed += n
	u.used += n
	return true
}

// Release returns n unused tokens to both global and per-user pools.
// Will not exceed original limits.
func (a *Allocator) Release(userID string, n int) {
	a.mu.Lock()
	defer a.mu.Unlock()

	a.globalUsed -= n
	if a.globalUsed < 0 {
		a.globalUsed = 0
	}

	if u, exists := a.users[userID]; exists {
		u.used -= n
		if u.used < 0 {
			u.used = 0
		}
	}
}

// SetUserQuota sets (or updates) the per-window quota limit for a user.
// If the user is new, their used count starts at 0.
// If the user exists, their limit is updated and used count is preserved
// (but capped at the new limit).
func (a *Allocator) SetUserQuota(userID string, limit int) {
	a.mu.Lock()
	defer a.mu.Unlock()

	if u, exists := a.users[userID]; exists {
		u.limit = limit
		if u.used > limit {
			u.used = limit
		}
	} else {
		a.users[userID] = &userEntry{limit: limit, used: 0}
	}
}

// RemoveUser removes a user's quota entry.
func (a *Allocator) RemoveUser(userID string) {
	a.mu.Lock()
	defer a.mu.Unlock()

	delete(a.users, userID)
}

// Remaining returns the remaining quota for a user in the current window.
// Returns 0 if user does not exist.
func (a *Allocator) Remaining(userID string) int {
	a.mu.Lock()
	defer a.mu.Unlock()

	u, exists := a.users[userID]
	if !exists {
		return 0
	}
	remaining := u.limit - u.used
	if remaining < 0 {
		return 0
	}
	return remaining
}

// GlobalRemaining returns the remaining global quota.
func (a *Allocator) GlobalRemaining() int {
	a.mu.Lock()
	defer a.mu.Unlock()

	remaining := a.globalMax - a.globalUsed
	if remaining < 0 {
		return 0
	}
	return remaining
}

// Refill resets all counters (global and per-user) for a new window.
func (a *Allocator) Refill() {
	a.mu.Lock()
	defer a.mu.Unlock()

	a.globalUsed = 0
	for _, u := range a.users {
		u.used = 0
	}
}

// StartRefill runs a background goroutine that calls Refill at the given
// interval. Stops when ctx is cancelled.
func (a *Allocator) StartRefill(ctx context.Context, interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				a.Refill()
			}
		}
	}()
}
