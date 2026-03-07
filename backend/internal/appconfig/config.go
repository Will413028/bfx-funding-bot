package appconfig

import (
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"os"

	"go.uber.org/fx"
)

type Config struct {
	Port        string
	FrontendURL string
	Environment string
	DatabaseURL string

	JWTPrivateKey *rsa.PrivateKey
	JWTPublicKey  *rsa.PublicKey
}

func Load() (Config, error) {
	cfg := Config{
		Port:        getEnvOrDefault("PORT", "8080"),
		FrontendURL: os.Getenv("FRONTEND_URL"),
		Environment: getEnvOrDefault("ENVIRONMENT", "development"),
		DatabaseURL: os.Getenv("DATABASE_URL"),
	}

	if cfg.FrontendURL == "" {
		return Config{}, fmt.Errorf("required environment variable FRONTEND_URL is not set")
	}
	if cfg.DatabaseURL == "" {
		return Config{}, fmt.Errorf("required environment variable DATABASE_URL is not set")
	}

	privPEM := os.Getenv("JWT_PRIVATE_KEY")
	pubPEM := os.Getenv("JWT_PUBLIC_KEY")
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
