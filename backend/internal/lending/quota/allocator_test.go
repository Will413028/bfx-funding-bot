package quota

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestAllocator_AcquireBothSufficient(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)

	ok := a.Acquire("u1", 3)
	if !ok {
		t.Fatal("expected Acquire to succeed")
	}
	if a.GlobalRemaining() != 87 {
		t.Errorf("global remaining: got %d, want 87", a.GlobalRemaining())
	}
	if a.Remaining("u1") != 12 {
		t.Errorf("user remaining: got %d, want 12", a.Remaining("u1"))
	}
}

func TestAllocator_AcquireGlobalInsufficient(t *testing.T) {
	a := NewAllocator(5)
	a.SetUserQuota("u1", 15)

	// Use most of global
	a.Acquire("u1", 4)

	ok := a.Acquire("u1", 3)
	if ok {
		t.Fatal("expected Acquire to fail (global insufficient)")
	}
	// Counters unchanged from failed acquire
	if a.GlobalRemaining() != 1 {
		t.Errorf("global remaining: got %d, want 1", a.GlobalRemaining())
	}
	if a.Remaining("u1") != 11 {
		t.Errorf("user remaining: got %d, want 11", a.Remaining("u1"))
	}
}

func TestAllocator_AcquireUserInsufficient(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 5)

	a.Acquire("u1", 4)

	ok := a.Acquire("u1", 3)
	if ok {
		t.Fatal("expected Acquire to fail (user insufficient)")
	}
	if a.Remaining("u1") != 1 {
		t.Errorf("user remaining: got %d, want 1", a.Remaining("u1"))
	}
}

func TestAllocator_AcquireUnknownUser(t *testing.T) {
	a := NewAllocator(90)

	ok := a.Acquire("u1", 1)
	if ok {
		t.Fatal("expected Acquire to fail for unknown user")
	}
}

func TestAllocator_Release(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)

	a.Acquire("u1", 5)
	if a.Remaining("u1") != 10 {
		t.Fatalf("after acquire: got %d, want 10", a.Remaining("u1"))
	}

	a.Release("u1", 2)
	if a.Remaining("u1") != 12 {
		t.Errorf("after release: got %d, want 12", a.Remaining("u1"))
	}
	if a.GlobalRemaining() != 87 {
		t.Errorf("global after release: got %d, want 87", a.GlobalRemaining())
	}
}

func TestAllocator_ReleaseDoesNotExceedMax(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)

	// Release more than used
	a.Acquire("u1", 2)
	a.Release("u1", 10) // release 10 but only used 2

	if a.Remaining("u1") != 15 {
		t.Errorf("user remaining: got %d, want 15 (capped at limit)", a.Remaining("u1"))
	}
	if a.GlobalRemaining() != 90 {
		t.Errorf("global remaining: got %d, want 90 (capped at max)", a.GlobalRemaining())
	}
}

func TestAllocator_SetUserQuotaNew(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)

	if a.Remaining("u1") != 15 {
		t.Errorf("remaining: got %d, want 15", a.Remaining("u1"))
	}
}

func TestAllocator_SetUserQuotaUpdate(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 10)
	a.Acquire("u1", 3) // used=3

	a.SetUserQuota("u1", 20)
	if a.Remaining("u1") != 17 {
		t.Errorf("remaining: got %d, want 17 (limit=20, used=3)", a.Remaining("u1"))
	}
}

func TestAllocator_SetUserQuotaDownsize(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)
	a.Acquire("u1", 12) // used=12

	a.SetUserQuota("u1", 10) // limit < used → used capped at 10
	if a.Remaining("u1") != 0 {
		t.Errorf("remaining: got %d, want 0 (used capped at new limit)", a.Remaining("u1"))
	}
}

func TestAllocator_RemoveUser(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)
	a.Acquire("u1", 5)

	a.RemoveUser("u1")

	if a.Remaining("u1") != 0 {
		t.Errorf("remaining after remove: got %d, want 0", a.Remaining("u1"))
	}
	// Acquire should fail
	ok := a.Acquire("u1", 1)
	if ok {
		t.Error("expected Acquire to fail after RemoveUser")
	}
}

func TestAllocator_Remaining_NonExistent(t *testing.T) {
	a := NewAllocator(90)
	if a.Remaining("unknown") != 0 {
		t.Errorf("remaining: got %d, want 0", a.Remaining("unknown"))
	}
}

func TestAllocator_Refill(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)
	a.SetUserQuota("u2", 20)

	a.Acquire("u1", 8)
	a.Acquire("u2", 5)

	if a.GlobalRemaining() != 77 {
		t.Fatalf("before refill: global=%d, want 77", a.GlobalRemaining())
	}

	a.Refill()

	if a.GlobalRemaining() != 90 {
		t.Errorf("after refill: global=%d, want 90", a.GlobalRemaining())
	}
	if a.Remaining("u1") != 15 {
		t.Errorf("after refill: u1=%d, want 15", a.Remaining("u1"))
	}
	if a.Remaining("u2") != 20 {
		t.Errorf("after refill: u2=%d, want 20", a.Remaining("u2"))
	}
}

func TestAllocator_StartRefill(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)
	a.Acquire("u1", 10)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	a.StartRefill(ctx, 50*time.Millisecond)

	// Wait for at least one refill
	time.Sleep(80 * time.Millisecond)

	if a.Remaining("u1") != 15 {
		t.Errorf("after auto-refill: got %d, want 15", a.Remaining("u1"))
	}
}

func TestAllocator_ConcurrentAcquire(t *testing.T) {
	a := NewAllocator(100)
	for i := 0; i < 10; i++ {
		a.SetUserQuota("u1", 100) // high per-user limit so global is the bottleneck
	}
	a.SetUserQuota("u1", 100)

	var acquired int64
	var wg sync.WaitGroup

	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if a.Acquire("u1", 3) {
				atomic.AddInt64(&acquired, 3)
			}
		}()
	}

	wg.Wait()

	total := atomic.LoadInt64(&acquired)
	if total > 100 {
		t.Errorf("total acquired: %d, exceeds global max 100", total)
	}
	// At least some should succeed
	if total == 0 {
		t.Error("expected some acquires to succeed")
	}
}

func TestAllocator_MultiUserFairness(t *testing.T) {
	a := NewAllocator(90)
	a.SetUserQuota("u1", 15)
	a.SetUserQuota("u2", 30)

	// u1 uses full quota
	for i := 0; i < 5; i++ {
		a.Acquire("u1", 3)
	}
	if a.Remaining("u1") != 0 {
		t.Errorf("u1 remaining: got %d, want 0", a.Remaining("u1"))
	}

	// u2 should still have quota (global has 75 left)
	ok := a.Acquire("u2", 10)
	if !ok {
		t.Error("u2 should still be able to acquire")
	}
	if a.Remaining("u2") != 20 {
		t.Errorf("u2 remaining: got %d, want 20", a.Remaining("u2"))
	}
}
