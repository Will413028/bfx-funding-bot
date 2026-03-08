package handler

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/will/bfx-funding-bot/backend/internal/domain"
	"github.com/will/bfx-funding-bot/backend/internal/middleware"
	"github.com/will/bfx-funding-bot/backend/internal/service"
)

type ExecutionHandler struct {
	svc *service.ExecutionService
}

func NewExecutionHandler(svc *service.ExecutionService) *ExecutionHandler {
	return &ExecutionHandler{svc: svc}
}

func (h *ExecutionHandler) List(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)

	// Cursor-based pagination mode (when "after" param is present)
	if after := c.Query("after"); after != "" {
		h.listCursor(c, userID, after)
		return
	}

	// Legacy mode (since + limit)
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

	limit := 50
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

	records, err := h.svc.ListByUser(c.Request.Context(), userID, since, limit)
	if err != nil {
		handleError(c, err)
		return
	}

	c.JSON(http.StatusOK, gin.H{"executions": records})
}

// ListPaginated handles cursor-based pagination for executions.
// Use this for the first page (no cursor) or subsequent pages (with cursor).
func (h *ExecutionHandler) ListPaginated(c *gin.Context) {
	userID := c.GetString(middleware.ContextUserID)
	h.listCursor(c, userID, c.Query("after"))
}

func (h *ExecutionHandler) listCursor(c *gin.Context, userID, after string) {
	limit := ClampLimit(parseIntQuery(c, "limit"), 20, 100)

	var cursorTime *time.Time
	var cursorID string
	if after != "" {
		cursor, err := DecodeCursor(after)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": gin.H{"code": "INVALID_PARAM", "message": "invalid cursor"},
			})
			return
		}
		cursorTime = &cursor.Timestamp
		cursorID = cursor.ID
	}

	records, err := h.svc.ListByUserPaginated(c.Request.Context(), userID, cursorTime, cursorID, limit+1)
	if err != nil {
		handleError(c, err)
		return
	}

	pagination := executionPagination(records, limit)
	if len(records) > limit {
		records = records[:limit]
	}

	c.JSON(http.StatusOK, gin.H{
		"executions": records,
		"pagination": pagination,
	})
}

func executionPagination(records []domain.ExecutionRecord, limit int) PaginationResponse {
	if len(records) <= limit {
		return PaginationResponse{HasMore: false}
	}
	last := records[limit-1]
	return PaginationResponse{
		NextCursor: EncodeCursor(last.CreatedAt, last.ID),
		HasMore:    true,
	}
}

func parseIntQuery(c *gin.Context, key string) int {
	if l := c.Query(key); l != "" {
		n, _ := strconv.Atoi(l)
		return n
	}
	return 0
}
