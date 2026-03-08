## Context

目前後端所有 API endpoint 沒有頻率限制。隨著功能增加（dashboard、earnings 等會 proxy 呼叫 Bitfinex API），需要在正式上線前加入基本防護。

現有 middleware chain：`RequestID → Recovery → Logger → CORS → (JWTAuth on protected routes)`。

## Goals / Non-Goals

**Goals:**
- 基於 IP 的請求頻率限制
- 公開路由（auth）與受保護路由使用不同限制策略
- Health check 不受限制
- 回傳標準 429 回應 + rate limit headers
- In-memory 實作，單機部署足夠

**Non-Goals:**
- 基於 User ID 的限制（需要在 JWT 解析後才能取得，未來可擴充）
- 分散式 rate limiting（Redis-based，待 Phase A3 Redis 整合後再考慮）
- 動態調整限制參數（先用固定設定，未來可改為 config-driven）
- 針對單一 endpoint 的細粒度限制

## Decisions

### 1. 演算法：Token Bucket（`golang.org/x/time/rate`）

**選擇**：Token Bucket
**替代方案**：
- Fixed Window Counter — 有邊界突發問題（兩個 window 交界處可短時間內通過 2x 限制）
- Sliding Window Log — 記憶體開銷大，需儲存每個請求的 timestamp
- Leaky Bucket — 固定處理速率，不允許任何 burst

**理由**：Token Bucket 允許合理的短暫 burst（使用者快速連續操作），同時維持長期平均速率。`golang.org/x/time/rate` 是 Go 官方擴充庫，成熟穩定，零外部依賴。

### 2. 限制策略：雙層

| 路由群組 | Rate (requests/sec) | Burst |
|----------|-------------------|-------|
| Public (`/auth/*`) | 5 r/s | 10 |
| Protected (其他 JWT 路由) | 20 r/s | 40 |

**理由**：
- Auth 路由較嚴格：防止暴力破解登入/註冊
- Protected 路由較寬鬆：已通過 JWT 驗證，dashboard/earnings 等頁面會同時發多個 API 呼叫

### 3. 識別方式：Client IP

使用 `c.ClientIP()`（Gin 內建，支援 `X-Forwarded-For`、`X-Real-IP`）。Koyeb reverse proxy 會設定這些 header。

每個 IP 有獨立的 `rate.Limiter` 實例，存放在 `sync.Map` 中。

### 4. 清理策略：定期清理閒置 Limiter

每 5 分鐘掃描一次，清理超過 10 分鐘沒有請求的 IP 的 limiter，避免記憶體洩漏。

### 5. Middleware 位置

插入在 CORS 之後、路由匹配之前。Health check 在 rate limit middleware 之前單獨註冊，不受限制。

```
RequestID → Recovery → Logger → CORS → RateLimit → [Router]
                                                      ├── /health (不受限)
                                                      ├── /auth/* (public 限制)
                                                      └── protected/* (protected 限制)
```

修正：Rate limiting 不作為全局 middleware，而是作為 route group middleware 分別套用到 public 和 protected group，health check 自然不受影響。

## Risks / Trade-offs

- **[風險] 多機部署時 rate limit 不共享** → 目前單機部署，未來 Phase A3 整合 Redis 後可改為分散式。單機下每台機器獨立限制已足夠。
- **[風險] Reverse proxy 後面所有請求來自同一 IP** → Koyeb 正確設定 `X-Forwarded-For`，Gin 的 `ClientIP()` 會優先讀取此 header。需在部署時設定 `gin.SetTrustedProxies()`。
- **[取捨] In-memory vs Redis** → 選擇 in-memory 避免在 Redis 整合前引入額外依賴。代價是重啟後限制器重置，但這是可接受的。
