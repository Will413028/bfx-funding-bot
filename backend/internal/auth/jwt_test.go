package auth

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

func testKeyPair(t *testing.T) (*rsa.PrivateKey, *rsa.PublicKey) {
	t.Helper()
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return priv, &priv.PublicKey
}

func TestGenerateAndValidateToken(t *testing.T) {
	priv, pub := testKeyPair(t)
	mgr := NewJWTManager(priv, pub)

	token, expiresAt, err := mgr.GenerateToken("user-123", "test@example.com")
	if err != nil {
		t.Fatalf("GenerateToken error: %v", err)
	}
	if token == "" {
		t.Fatal("expected non-empty token")
	}
	if time.Until(expiresAt) < 14*time.Minute {
		t.Fatalf("expected expiry ~15min from now, got %v", expiresAt)
	}

	claims, err := mgr.ValidateToken(token)
	if err != nil {
		t.Fatalf("ValidateToken error: %v", err)
	}
	if claims.Subject != "user-123" {
		t.Errorf("expected subject user-123, got %s", claims.Subject)
	}
	if claims.Email != "test@example.com" {
		t.Errorf("expected email test@example.com, got %s", claims.Email)
	}
}

func TestValidateToken_Tampered(t *testing.T) {
	priv, pub := testKeyPair(t)
	mgr := NewJWTManager(priv, pub)

	token, _, _ := mgr.GenerateToken("user-123", "test@example.com")

	// Tamper with signature by flipping a byte in the middle of the signature
	parts := bytes.Split([]byte(token), []byte("."))
	sig := parts[2]
	sig[len(sig)/2] ^= 0xff
	tampered := string(parts[0]) + "." + string(parts[1]) + "." + string(sig)
	_, err := mgr.ValidateToken(tampered)
	if err == nil {
		t.Fatal("expected error for tampered token")
	}
}

func TestValidateToken_WrongKey(t *testing.T) {
	priv1, _ := testKeyPair(t)
	_, pub2 := testKeyPair(t)

	mgr1 := NewJWTManager(priv1, pub2) // sign with key1, verify with key2

	token, _, _ := mgr1.GenerateToken("user-123", "test@example.com")
	_, err := mgr1.ValidateToken(token)
	if err == nil {
		t.Fatal("expected error for wrong key")
	}
}

func TestValidateToken_Expired(t *testing.T) {
	priv, pub := testKeyPair(t)

	// Manually create an expired token
	claims := Claims{
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   "user-123",
			IssuedAt:  jwt.NewNumericDate(time.Now().Add(-48 * time.Hour)),
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(-24 * time.Hour)),
		},
		Email: "test@example.com",
	}
	token := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	signed, err := token.SignedString(priv)
	if err != nil {
		t.Fatal(err)
	}

	mgr := NewJWTManager(priv, pub)
	_, err = mgr.ValidateToken(signed)
	if err == nil {
		t.Fatal("expected error for expired token")
	}
}
