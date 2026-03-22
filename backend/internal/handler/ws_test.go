package handler

import (
	"sync"
	"testing"
)

func TestClient_CloseSendIdempotent(t *testing.T) {
	c := &client{
		send: make(chan []byte, 1),
	}

	// Calling closeSend multiple times must not panic.
	c.closeSend()
	c.closeSend()
	c.closeSend()
}

func TestClient_CloseSendConcurrent(t *testing.T) {
	c := &client{
		send: make(chan []byte, 1),
	}

	var wg sync.WaitGroup
	for range 50 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c.closeSend()
		}()
	}
	wg.Wait()
}

func TestHub_ShutdownUnregisterNoLeak(t *testing.T) {
	done := make(chan struct{})

	c := &client{
		send:   make(chan []byte, 1),
		userID: "test",
	}

	// Simulate readPump's defer: select between unregister and done.
	unregister := make(chan *client)
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		select {
		case unregister <- c:
		case <-done:
		}
	}()

	// Close done to simulate Hub shutdown — goroutine should not block.
	close(done)
	wg.Wait()
}
