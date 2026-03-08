package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type EarningsHandler struct {
	svc *service.EarningsService
}

func NewEarningsHandler(svc *service.EarningsService) *EarningsHandler {
	return &EarningsHandler{svc: svc}
}

func (h *EarningsHandler) Get(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	summary, err := h.svc.GetEarnings(c.Request.Context(), userID)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, summary)
}
