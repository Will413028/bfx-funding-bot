package bitfinex

import (
	"crypto/hmac"
	"crypto/sha512"
	"encoding/hex"
	"fmt"
	"sync/atomic"
	"time"
)

var nonceCounter atomic.Int64

func generateNonce() string {
	ts := time.Now().UnixMicro()
	seq := nonceCounter.Add(1)
	return fmt.Sprintf("%d%d", ts, seq)
}

func computeSignature(apiPath, nonce, body, apiSecret string) string {
	payload := "/api/" + apiPath + nonce + body
	mac := hmac.New(sha512.New384, []byte(apiSecret))
	mac.Write([]byte(payload))
	return hex.EncodeToString(mac.Sum(nil))
}

// computeWSSignature computes the HMAC-SHA384 signature for WebSocket authentication.
// WS payload format: "AUTH" + nonce (differs from REST: "/api/" + path + nonce + body).
func computeWSSignature(nonce, apiSecret string) (authPayload, signature string) {
	authPayload = "AUTH" + nonce
	mac := hmac.New(sha512.New384, []byte(apiSecret))
	mac.Write([]byte(authPayload))
	signature = hex.EncodeToString(mac.Sum(nil))
	return
}
