package bitfinex

import (
	"testing"
)

func TestComputeSignature(t *testing.T) {
	// Test vector: known inputs → deterministic HMAC-SHA384 output
	apiPath := "v2/auth/r/wallets"
	nonce := "1234567890000"
	body := "{}"
	secret := "test-secret-key"

	sig := computeSignature(apiPath, nonce, body, secret)

	// Verify it's a valid hex string of correct length (SHA384 = 96 hex chars)
	if len(sig) != 96 {
		t.Errorf("expected signature length 96, got %d", len(sig))
	}

	// Same inputs must produce same output
	sig2 := computeSignature(apiPath, nonce, body, secret)
	if sig != sig2 {
		t.Error("same inputs produced different signatures")
	}

	// Different secret must produce different output
	sig3 := computeSignature(apiPath, nonce, body, "different-secret")
	if sig == sig3 {
		t.Error("different secrets produced same signature")
	}

	// Different body must produce different output
	sig4 := computeSignature(apiPath, nonce, `{"type":"LIMIT"}`, secret)
	if sig == sig4 {
		t.Error("different bodies produced same signature")
	}
}

func TestGenerateNonce(t *testing.T) {
	nonce1 := generateNonce()
	nonce2 := generateNonce()

	if nonce1 == "" || nonce2 == "" {
		t.Error("nonce should not be empty")
	}
	if nonce1 == nonce2 {
		t.Error("consecutive nonces should be unique")
	}
}
