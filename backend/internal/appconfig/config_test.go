package appconfig

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"strings"
	"testing"
)

// generateTestKeyPair generates a small RSA key pair for testing.
func generateTestKeyPair(t *testing.T) (privPEM, pubPEM string) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}

	privBytes, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatalf("marshal private key: %v", err)
	}
	privBlock := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privBytes})

	pubBytes, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatalf("marshal public key: %v", err)
	}
	pubBlock := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubBytes})

	return string(privBlock), string(pubBlock)
}

// setRequiredEnvVars sets all required env vars to valid values.
func setRequiredEnvVars(t *testing.T) {
	t.Helper()
	privPEM, pubPEM := generateTestKeyPair(t)
	aesKey := hex.EncodeToString(make([]byte, 32)) // 64 hex chars = 32 bytes

	t.Setenv("FRONTEND_URL", "http://localhost:3000")
	t.Setenv("DATABASE_URL", "postgres://localhost/test")
	t.Setenv("REDIS_URL", "redis://localhost:6379")
	t.Setenv("JWT_PRIVATE_KEY", privPEM)
	t.Setenv("JWT_PUBLIC_KEY", pubPEM)
	t.Setenv("AES_KEY", aesKey)
	t.Setenv("RESEND_API_KEY", "re_test_key")
}

func TestLoad_Success(t *testing.T) {
	setRequiredEnvVars(t)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error: %v", err)
	}
	if cfg.FrontendURL != "http://localhost:3000" {
		t.Errorf("FrontendURL = %q, want %q", cfg.FrontendURL, "http://localhost:3000")
	}
	if cfg.JWTPrivateKey == nil {
		t.Error("JWTPrivateKey is nil")
	}
	if cfg.JWTPublicKey == nil {
		t.Error("JWTPublicKey is nil")
	}
	if len(cfg.AESKey) != 32 {
		t.Errorf("AESKey length = %d, want 32", len(cfg.AESKey))
	}
}

func TestLoad_DefaultPort(t *testing.T) {
	setRequiredEnvVars(t)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error: %v", err)
	}
	if cfg.Port != "8080" {
		t.Errorf("Port = %q, want %q", cfg.Port, "8080")
	}
}

func TestLoad_MissingFrontendURL(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("FRONTEND_URL", "")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error when FRONTEND_URL is empty")
	}
}

func TestLoad_MissingDatabaseURL(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("DATABASE_URL", "")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error when DATABASE_URL is empty")
	}
}

func TestLoad_MissingRedisURL(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("REDIS_URL", "")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error when REDIS_URL is empty")
	}
}

func TestLoad_MissingJWTKeys(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("JWT_PRIVATE_KEY", "")
	t.Setenv("JWT_PUBLIC_KEY", "")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error when JWT keys are empty")
	}
}

func TestLoad_MalformedPrivateKey(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("JWT_PRIVATE_KEY", "not-a-pem-block")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error with malformed private key")
	}
}

func TestLoad_MalformedPublicKey(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("JWT_PUBLIC_KEY", "not-a-pem-block")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error with malformed public key")
	}
}

func TestLoad_MissingAESKey(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("AES_KEY", "")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error when AES_KEY is empty")
	}
}

func TestLoad_InvalidAESKeyHex(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("AES_KEY", "not-hex-at-all")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error with invalid AES_KEY hex")
	}
}

func TestLoad_WrongAESKeyLength(t *testing.T) {
	setRequiredEnvVars(t)
	// 16 bytes instead of 32
	t.Setenv("AES_KEY", hex.EncodeToString(make([]byte, 16)))

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error when AES_KEY is not 32 bytes")
	}
}

func TestLoad_MissingResendAPIKey(t *testing.T) {
	setRequiredEnvVars(t)
	t.Setenv("RESEND_API_KEY", "")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() should error when RESEND_API_KEY is empty")
	}
}

func TestLoad_EscapedNewlinesInPEM(t *testing.T) {
	setRequiredEnvVars(t)

	// Simulate escaped newlines (as they'd come from env var)
	privPEM, pubPEM := generateTestKeyPair(t)
	escaped := func(s string) string {
		return strings.ReplaceAll(s, "\n", `\n`)
	}
	t.Setenv("JWT_PRIVATE_KEY", escaped(privPEM))
	t.Setenv("JWT_PUBLIC_KEY", escaped(pubPEM))

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() should handle escaped newlines: %v", err)
	}
	if cfg.JWTPrivateKey == nil {
		t.Error("JWTPrivateKey should be parsed from escaped PEM")
	}
}
