package appconfig

import (
	"fmt"
	"os"

	"go.uber.org/fx"
)

type Config struct {
	Port        string
	FrontendURL string
	Environment string
}

func Load() (Config, error) {
	cfg := Config{
		Port:        getEnvOrDefault("PORT", "8080"),
		FrontendURL: os.Getenv("FRONTEND_URL"),
		Environment: getEnvOrDefault("ENVIRONMENT", "development"),
	}

	if cfg.FrontendURL == "" {
		return Config{}, fmt.Errorf("required environment variable FRONTEND_URL is not set")
	}

	return cfg, nil
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
