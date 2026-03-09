package handler

import (
	"net/http"

	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"

	"github.com/will/bfx-funding-bot/backend/internal/appconfig"
	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
)

func NewRouter(cfg appconfig.Config, log *zap.Logger, jwtMgr *auth.JWTManager, health *HealthHandler, authH *AuthHandler, userH *UserHandler, apiKeyH *APIKeyHandler, configH *ConfigHandler, dashH *DashboardHandler, earnH *EarningsHandler, execH *ExecutionHandler, billH *BillingHandler, wsTokenH *WSTokenHandler, hub *Hub) *gin.Engine {
	if cfg.Environment == "production" {
		gin.SetMode(gin.ReleaseMode)
	}

	r := gin.New()

	// Middleware chain
	r.Use(middleware.RequestID())
	r.Use(middleware.Recovery(log))
	r.Use(middleware.Logger(log))
	r.Use(cors.New(cors.Config{
		AllowOrigins:     []string{cfg.FrontendURL},
		AllowMethods:     []string{"GET", "POST", "PUT", "DELETE", "OPTIONS"},
		AllowHeaders:     []string{"Origin", "Content-Type", "Authorization", "X-Request-ID"},
		AllowCredentials: true,
	}))

	// 404 handler
	r.NoRoute(func(c *gin.Context) {
		c.JSON(http.StatusNotFound, gin.H{
			"error": gin.H{
				"code":    "NOT_FOUND",
				"message": "Route not found",
			},
		})
	})

	// API v1 routes
	v1 := r.Group("/api/v1")
	{
		// Health check — no rate limiting
		v1.GET("/health", health.Status)

		// Public auth routes — strict rate limit (5 r/s, burst 10)
		authGroup := v1.Group("/auth", middleware.RateLimit(5, 10))
		{
			authGroup.POST("/register", authH.Register)
			authGroup.POST("/login", authH.Login)
		}

		// WebSocket upgrade — token-based auth (no JWT middleware)
		v1.GET("/ws", hub.HandleWS)

		// Protected routes (JWT required) — relaxed rate limit (20 r/s, burst 40)
		protected := v1.Group("", middleware.RateLimit(20, 40), middleware.JWTAuth(jwtMgr))
		{
			protected.GET("/me", userH.GetProfile)
			protected.PUT("/me/password", userH.ChangePassword)

			protected.POST("/api-keys", apiKeyH.Create)
			protected.GET("/api-keys", apiKeyH.List)
			protected.GET("/apikeys/:id", apiKeyH.GetByID)
			protected.DELETE("/apikeys/:id", apiKeyH.Delete)
			protected.POST("/apikeys/:id/verify", apiKeyH.Verify)

			protected.PUT("/configs", configH.Save)
			protected.GET("/configs", configH.Get)
			protected.DELETE("/configs", configH.Delete)

			protected.GET("/dashboard", dashH.Get)
			protected.GET("/earnings", earnH.Get)
			protected.GET("/executions", execH.List)

			protected.GET("/billing", billH.Get)
			protected.GET("/billing/plan", billH.GetPlan)

			protected.POST("/auth/ws-token", wsTokenH.Create)
		}
	}

	return r
}
