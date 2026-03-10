package appconfig

import (
	"crypto/rsa"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"os"
	"strings"

	"go.uber.org/fx"
)

type Config struct {
	Port        string
	FrontendURL string
	Environment string
	DatabaseURL string
	RedisURL    string

	JWTPrivateKey *rsa.PrivateKey
	JWTPublicKey  *rsa.PublicKey

	AESKey []byte

	ResendAPIKey          string
	NotificationFromEmail string

	AxiomToken   string
	AxiomDataset string
}

func Load() (Config, error) {
	cfg := Config{
		Port:        getEnvOrDefault("PORT", "8080"),
		FrontendURL: os.Getenv("FRONTEND_URL"),
		Environment: getEnvOrDefault("ENVIRONMENT", "development"),
		DatabaseURL: os.Getenv("DATABASE_URL"),
		RedisURL:    os.Getenv("REDIS_URL"),
	}

	if cfg.FrontendURL == "" {
		return Config{}, fmt.Errorf("required environment variable FRONTEND_URL is not set")
	}
	if cfg.DatabaseURL == "" {
		return Config{}, fmt.Errorf("required environment variable DATABASE_URL is not set")
	}
	if cfg.RedisURL == "" {
		return Config{}, fmt.Errorf("required environment variable REDIS_URL is not set")
	}

	privPEM := strings.ReplaceAll(os.Getenv("JWT_PRIVATE_KEY"), `\n`, "\n")
	pubPEM := strings.ReplaceAll(os.Getenv("JWT_PUBLIC_KEY"), `\n`, "\n")
	if privPEM == "" || pubPEM == "" {
		return Config{}, fmt.Errorf("required environment variables JWT_PRIVATE_KEY and JWT_PUBLIC_KEY are not set")
	}

	privKey, err := parseRSAPrivateKey(privPEM)
	if err != nil {
		return Config{}, fmt.Errorf("parsing JWT_PRIVATE_KEY: %w", err)
	}
	pubKey, err := parseRSAPublicKey(pubPEM)
	if err != nil {
		return Config{}, fmt.Errorf("parsing JWT_PUBLIC_KEY: %w", err)
	}

	cfg.JWTPrivateKey = privKey
	cfg.JWTPublicKey = pubKey

	aesHex := os.Getenv("AES_KEY")
	if aesHex == "" {
		return Config{}, fmt.Errorf("required environment variable AES_KEY is not set")
	}
	aesKey, err := hex.DecodeString(aesHex)
	if err != nil {
		return Config{}, fmt.Errorf("parsing AES_KEY: invalid hex: %w", err)
	}
	if len(aesKey) != 32 {
		return Config{}, fmt.Errorf("parsing AES_KEY: expected 32 bytes, got %d", len(aesKey))
	}
	cfg.AESKey = aesKey

	cfg.ResendAPIKey = os.Getenv("RESEND_API_KEY")
	if cfg.ResendAPIKey == "" {
		return Config{}, fmt.Errorf("required environment variable RESEND_API_KEY is not set")
	}
	cfg.NotificationFromEmail = getEnvOrDefault("NOTIFICATION_FROM_EMAIL", "noreply@bfx-funding.bot")

	cfg.AxiomToken = os.Getenv("AXIOM_TOKEN")
	cfg.AxiomDataset = os.Getenv("AXIOM_DATASET")

	return cfg, nil
}

func parseRSAPrivateKey(pemStr string) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode([]byte(pemStr))
	if block == nil {
		return nil, fmt.Errorf("no PEM block found")
	}
	return x509.ParsePKCS1PrivateKey(block.Bytes)
}

func parseRSAPublicKey(pemStr string) (*rsa.PublicKey, error) {
	block, _ := pem.Decode([]byte(pemStr))
	if block == nil {
		return nil, fmt.Errorf("no PEM block found")
	}
	pub, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	rsaPub, ok := pub.(*rsa.PublicKey)
	if !ok {
		return nil, fmt.Errorf("not an RSA public key")
	}
	return rsaPub, nil
}

var Module = fx.Module("appconfig",
	fx.Provide(Load),
)

func getEnvOrDefault(key, defaultValue string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultValue
}
