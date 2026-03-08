## Context

Upstash Redis (Serverless)，使用 TLS 連線（`rediss://`）。go-redis v9 原生支援 `rediss://` scheme 自動啟用 TLS。

## Goals / Non-Goals

**Goals:**
- Redis client 初始化 + fx Lifecycle close hook
- Health check endpoint 加入 Redis PING
- `REDIS_URL` 環境變數整合

**Non-Goals:**
- Session 儲存（未來 change）
- MarketSnapshot 快取（Phase B）
- Pub/Sub（Phase B）

## Decisions

### 1. 使用 `redis.ParseURL` 解析連線字串

go-redis 的 `ParseURL` 支援 `rediss://` scheme 自動設定 TLS，無需手動配置 TLS config。

### 2. Health check 用 PING

`client.Ping(ctx)` 是最輕量的 connectivity check。回傳在 health response 的 `redis` 欄位。

### 3. REDIS_URL 為 required

Redis 是後續多個功能的基礎，設為必填。開發環境可用本地 Redis 或 Upstash free tier。

## Risks / Trade-offs

- **[風險] Upstash free tier 有連線數限制** → 單一 `redis.Client` 即可，go-redis 內建 connection pool。
