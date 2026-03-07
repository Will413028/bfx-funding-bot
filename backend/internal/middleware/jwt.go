package middleware

import (
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"

	"github.com/will/bfx-funding-bot/backend/internal/auth"
	"github.com/will/bfx-funding-bot/backend/internal/domain"
)

const (
	ContextUserID = "user_id"
	ContextEmail  = "user_email"
)

func JWTAuth(jwtMgr *auth.JWTManager) gin.HandlerFunc {
	return func(c *gin.Context) {
		header := c.GetHeader("Authorization")
		if header == "" {
			abortWithAppError(c, domain.ErrMissingToken())
			return
		}

		tokenString, ok := strings.CutPrefix(header, "Bearer ")
		if !ok || tokenString == "" {
			abortWithAppError(c, domain.ErrInvalidToken())
			return
		}

		claims, err := jwtMgr.ValidateToken(tokenString)
		if err != nil {
			if isExpiredError(err) {
				abortWithAppError(c, domain.ErrTokenExpired())
			} else {
				abortWithAppError(c, domain.ErrInvalidToken())
			}
			return
		}

		c.Set(ContextUserID, claims.Subject)
		c.Set(ContextEmail, claims.Email)
		c.Next()
	}
}

func abortWithAppError(c *gin.Context, appErr *domain.AppError) {
	c.AbortWithStatusJSON(appErr.StatusCode, gin.H{
		"error": gin.H{"code": appErr.Code, "message": appErr.Message},
	})
}

func isExpiredError(err error) bool {
	return strings.Contains(err.Error(), jwt.ErrTokenExpired.Error())
}
