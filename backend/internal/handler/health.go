package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

// EngineHealthProvider exposes engine status for health checks.
type EngineHealthProvider interface {
	Status() domain.EngineStatus
}

type HealthHandler struct {
	pool   *pgxpool.Pool
	redis  *redis.Client
	engine EngineHealthProvider
}

func NewHealthHandler(pool *pgxpool.Pool, redis *redis.Client, engine EngineHealthProvider) *HealthHandler {
	return &HealthHandler{pool: pool, redis: redis, engine: engine}
}

func (h *HealthHandler) Status(c *gin.Context) {
	ctx := c.Request.Context()
	status := "ok"

	pgStatus := "ok"
	if h.pool != nil {
		if err := h.pool.Ping(ctx); err != nil {
			pgStatus = "error"
			status = "degraded"
		}
	}

	redisStatus := "ok"
	if h.redis != nil {
		if err := h.redis.Ping(ctx).Err(); err != nil {
			redisStatus = "error"
			status = "degraded"
		}
	}

	code := http.StatusOK
	if status != "ok" {
		code = http.StatusServiceUnavailable
	}

	resp := gin.H{
		"status":   status,
		"postgres": pgStatus,
		"redis":    redisStatus,
	}

	if h.engine != nil {
		resp["engine"] = h.engine.Status()
	}

	c.JSON(code, gin.H{"data": resp})
}
