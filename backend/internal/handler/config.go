package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type ConfigHandler struct {
	svc *service.ConfigService
}

func NewConfigHandler(svc *service.ConfigService) *ConfigHandler {
	return &ConfigHandler{svc: svc}
}

func (h *ConfigHandler) Save(c *gin.Context) {
	var cfg domain.StrategyConfig
	if err := c.ShouldBindJSON(&cfg); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": gin.H{"code": "VALIDATION_ERROR", "message": "Invalid request body"},
		})
		return
	}

	userID := c.GetString(middleware.ContextUserID)
	uc, err := h.svc.Save(c.Request.Context(), userID, cfg)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"data": gin.H{
			"id":        uc.ID,
			"userId":    uc.UserID,
			"config":    uc.Config,
			"createdAt": uc.CreatedAt,
			"updatedAt": uc.UpdatedAt,
		},
	})
}

func (h *ConfigHandler) Get(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	uc, err := h.svc.Get(c.Request.Context(), userID)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"data": gin.H{
			"id":        uc.ID,
			"userId":    uc.UserID,
			"config":    uc.Config,
			"createdAt": uc.CreatedAt,
			"updatedAt": uc.UpdatedAt,
		},
	})
}

func (h *ConfigHandler) Delete(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	if err := h.svc.Delete(c.Request.Context(), userID); err != nil {
		handleError(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}
