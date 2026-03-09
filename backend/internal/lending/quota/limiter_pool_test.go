package quota

import (
	"sync"
	"testing"
)

func TestGet_CreatesNewLimiterForUnknownUser(t *testing.T) {
	pool := NewRateLimiterPool(10, 5)

	l := pool.Get("user-1")
	if l == nil {
		t.Fatal("expected non-nil limiter for new user")
	}
}

func TestGet_ReturnsSameLimiterForKnownUser(t *testing.T) {
	pool := NewRateLimiterPool(10, 5)

	l1 := pool.Get("user-1")
	l2 := pool.Get("user-1")

	if l1 != l2 {
		t.Fatal("expected same limiter instance for the same user")
	}
}

func TestGet_ReturnsDifferentLimitersForDifferentUsers(t *testing.T) {
	pool := NewRateLimiterPool(10, 5)

	l1 := pool.Get("user-1")
	l2 := pool.Get("user-2")

	if l1 == l2 {
		t.Fatal("expected different limiter instances for different users")
	}
}

func TestRemove_DeletesLimiter(t *testing.T) {
	pool := NewRateLimiterPool(10, 5)

	l1 := pool.Get("user-1")
	pool.Remove("user-1")
	l2 := pool.Get("user-1")

	if l1 == l2 {
		t.Fatal("expected a new limiter after Remove, got the same instance")
	}
}

func TestRemove_NoOpForUnknownUser(t *testing.T) {
	pool := NewRateLimiterPool(10, 5)

	// Should not panic.
	pool.Remove("nonexistent")
}

func TestConcurrentAccess(t *testing.T) {
	pool := NewRateLimiterPool(10, 5)
	const goroutines = 100

	var wg sync.WaitGroup
	wg.Add(goroutines)

	for i := 0; i < goroutines; i++ {
		go func(id int) {
			defer wg.Done()
			userID := "user-shared"
			l := pool.Get(userID)
			if l == nil {
				t.Error("expected non-nil limiter")
			}
			// Half of the goroutines also remove to stress the lock.
			if id%2 == 0 {
				pool.Remove(userID)
			}
		}(i)
	}

	wg.Wait()
}
