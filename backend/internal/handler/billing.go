package handler

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type BillingHandler struct {
	svc *service.BillingService
}

func NewBillingHandler(svc *service.BillingService) *BillingHandler {
	return &BillingHandler{svc: svc}
}

func (h *BillingHandler) Get(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)

	var since time.Time
	if s := c.Query("since"); s != "" {
		t, err := time.Parse(time.RFC3339, s)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": gin.H{"code": "INVALID_PARAM", "message": "since must be RFC3339 format"},
			})
			return
		}
		since = t
	}

	limit := 12
	if l := c.Query("limit"); l != "" {
		n, err := strconv.Atoi(l)
		if err != nil || n < 1 {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": gin.H{"code": "INVALID_PARAM", "message": "limit must be a positive integer"},
			})
			return
		}
		limit = n
	}

	summary, err := h.svc.GetBilling(c.Request.Context(), userID, since, limit)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, summary)
}

func (h *BillingHandler) GetPlan(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)

	features, err := h.svc.GetPlan(c.Request.Context(), userID)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, features)
}
