package crypto

import (
	"bytes"
	"crypto/rand"
	"testing"
)

func testKey(t *testing.T) []byte {
	t.Helper()
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		t.Fatal(err)
	}
	return key
}

func TestEncryptDecryptRoundtrip(t *testing.T) {
	a, err := NewAES(testKey(t))
	if err != nil {
		t.Fatal(err)
	}

	plaintext := []byte("my-secret-api-key-value")
	ct, err := a.Encrypt(plaintext)
	if err != nil {
		t.Fatal(err)
	}

	decrypted, err := a.Decrypt(ct)
	if err != nil {
		t.Fatal(err)
	}

	if !bytes.Equal(plaintext, decrypted) {
		t.Fatalf("expected %q, got %q", plaintext, decrypted)
	}
}

func TestEncryptRandomNonce(t *testing.T) {
	a, err := NewAES(testKey(t))
	if err != nil {
		t.Fatal(err)
	}

	plaintext := []byte("same-data")
	ct1, _ := a.Encrypt(plaintext)
	ct2, _ := a.Encrypt(plaintext)

	if bytes.Equal(ct1, ct2) {
		t.Fatal("two encryptions of same plaintext should produce different ciphertexts")
	}
}

func TestDecryptTampered(t *testing.T) {
	a, err := NewAES(testKey(t))
	if err != nil {
		t.Fatal(err)
	}

	ct, _ := a.Encrypt([]byte("secret"))
	ct[len(ct)-1] ^= 0xff // tamper with last byte

	_, err = a.Decrypt(ct)
	if err == nil {
		t.Fatal("expected error for tampered ciphertext")
	}
}

func TestNewAES_InvalidKeySize(t *testing.T) {
	_, err := NewAES([]byte("too-short"))
	if err == nil {
		t.Fatal("expected error for invalid key size")
	}

	longKey := make([]byte, 64)
	_, err = NewAES(longKey)
	if err == nil {
		t.Fatal("expected error for 64-byte key")
	}
}

func TestCiphertextFormat(t *testing.T) {
	a, err := NewAES(testKey(t))
	if err != nil {
		t.Fatal(err)
	}

	ct, _ := a.Encrypt([]byte("data"))
	// GCM nonce is 12 bytes, ciphertext must be longer than that
	if len(ct) <= 12 {
		t.Fatalf("ciphertext too short: %d bytes", len(ct))
	}
}
