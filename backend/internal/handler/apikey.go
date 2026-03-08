package handler

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type APIKeyHandler struct {
	svc *service.APIKeyService
}

func NewAPIKeyHandler(svc *service.APIKeyService) *APIKeyHandler {
	return &APIKeyHandler{svc: svc}
}

type createAPIKeyRequest struct {
	APIKey    string `json:"api_key" binding:"required"`
	APISecret string `json:"api_secret" binding:"required"`
	Label     string `json:"label"`
}

func (h *APIKeyHandler) Create(c *gin.Context) {
	var req createAPIKeyRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": gin.H{"code": "VALIDATION_ERROR", "message": "api_key and api_secret are required"},
		})
		return
	}

	userID := c.GetString(middleware.ContextUserID)
	key, vr, err := h.svc.Create(c.Request.Context(), userID, req.APIKey, req.APISecret, req.Label)
	if err != nil {
		handleError(c, err)
		return
	}

	resp := gin.H{
		"id":              key.ID,
		"label":           key.Label,
		"api_key":         key.APIKey,
		"api_secret":      "****",
		"exchange_status": key.ExchangeStatus,
		"created_at":      key.CreatedAt,
	}
	if vr != nil && vr.FundingBalance != nil {
		resp["funding_balance"] = gin.H{
			"currency":  vr.FundingBalance.Currency,
			"balance":   vr.FundingBalance.Balance,
			"available": vr.FundingBalance.BalanceAvailable,
		}
	}

	c.JSON(http.StatusCreated, resp)
}

func (h *APIKeyHandler) List(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	keys, err := h.svc.List(c.Request.Context(), userID)
	if err != nil {
		handleError(c, err)
		return
	}

	result := make([]gin.H, len(keys))
	for i, k := range keys {
		result[i] = gin.H{
			"id":              k.ID,
			"label":           k.Label,
			"api_key":         k.APIKey,
			"api_secret":      "****",
			"exchange_status": k.ExchangeStatus,
			"created_at":      k.CreatedAt,
		}
	}
	c.JSON(http.StatusOK, result)
}

func (h *APIKeyHandler) GetByID(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	keyID := c.Param("id")

	key, err := h.svc.GetByID(c.Request.Context(), userID, keyID)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"id":              key.ID,
		"label":           key.Label,
		"api_key":         key.APIKey,
		"api_secret":      "****",
		"exchange_status": key.ExchangeStatus,
		"created_at":      key.CreatedAt,
	})
}

func (h *APIKeyHandler) Verify(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	keyID := c.Param("id")

	vr, err := h.svc.Verify(c.Request.Context(), userID, keyID)
	if err != nil {
		handleError(c, err)
		return
	}

	resp := gin.H{
		"status": vr.Status,
	}
	if vr.Error != "" {
		resp["error"] = vr.Error
	}
	if vr.FundingBalance != nil {
		resp["funding_balance"] = gin.H{
			"currency":  vr.FundingBalance.Currency,
			"balance":   vr.FundingBalance.Balance,
			"available": vr.FundingBalance.BalanceAvailable,
		}
	}

	c.JSON(http.StatusOK, resp)
}

func (h *APIKeyHandler) Delete(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	keyID := c.Param("id")

	if err := h.svc.Delete(c.Request.Context(), userID, keyID); err != nil {
		handleError(c, err)
		return
	}

	c.Status(http.StatusNoContent)
}
