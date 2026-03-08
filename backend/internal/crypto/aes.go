package crypto

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"fmt"
	"io"
)

type AES struct {
	gcm cipher.AEAD
}

func NewAES(key []byte) (*AES, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("AES key must be 32 bytes, got %d", len(key))
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return &AES{gcm: gcm}, nil
}

func (a *AES) Encrypt(plaintext []byte) ([]byte, error) {
	nonce := make([]byte, a.gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, err
	}
	return a.gcm.Seal(nonce, nonce, plaintext, nil), nil
}

func (a *AES) Decrypt(ciphertext []byte) ([]byte, error) {
	nonceSize := a.gcm.NonceSize()
	if len(ciphertext) < nonceSize {
		return nil, fmt.Errorf("ciphertext too short")
	}
	nonce, ct := ciphertext[:nonceSize], ciphertext[nonceSize:]
	return a.gcm.Open(nil, nonce, ct, nil)
}
