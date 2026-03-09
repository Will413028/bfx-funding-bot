## Context

F12 已建立 WebSocket 後端：`POST /api/v1/auth/ws-token`（30 秒 token）+ `GET /api/v1/ws?token=xxx`（升級 + snapshot 廣播）。前端已有 `NEXT_PUBLIC_WS_URL` env var、`MarketSnapshot` type、Zustand 套件（未使用）。Overview 頁面透過 `useDashboard()` REST hook 取得初始資料。

## Goals / Non-Goals

**Goals:**
- WebSocket client with auto-reconnect + exponential backoff
- Zustand store for WS connection state + latest snapshot
- useDashboardWS hook 整合到 Overview page
- WS snapshot 同步到 React Query cache（MarketPanel 即時更新）

**Non-Goals:**
- 不取代 REST 初始載入（WS 是增量疊加）
- 不實作 WS 雙向通訊（只接收 snapshot）
- 不實作離線佇列或訊息持久化
- 不處理 Worker status 推送（僅 MarketSnapshot）

## Decisions

### D1: 原生 WebSocket API

使用瀏覽器原生 `WebSocket`，不引入 `reconnecting-websocket` 等套件。自動重連邏輯簡單（指數退避 1s→30s），不值得增加依賴。

### D2: WS Token 透過 API Proxy 取得

`POST /api/proxy/auth/ws-token` 走既有的 cookie-based 代理，取得短期 token 後直接連接 `NEXT_PUBLIC_WS_URL`（後端 WS 端點，繞過 proxy）。

### D3: Zustand Store 結構

```typescript
interface WSState {
  snapshot: MarketSnapshot | null;
  status: "disconnected" | "connecting" | "connected";
  connect: () => void;
  disconnect: () => void;
}
```

Store 是 singleton，整個 app 共享。`connect()` 內部處理 token 取得 + WS 連線 + 重連。

### D4: React Query Cache Sync

WS 收到 snapshot 時，用 `queryClient.setQueryData()` 更新 dashboard cache 的 `market` 欄位，讓 `MarketPanel` 自動 re-render。不修改 `useDashboard` hook 本身。

### D5: 連線生命週期

- Overview page mount → `useDashboardWS()` → `connect()`
- Page unmount → `disconnect()`
- Token 過期 / 連線斷開 → 自動重連（先取新 token，再 reconnect）
- 重連間隔：1s → 2s → 4s → 8s → 16s → 30s（上限）

### D6: 檔案結構

```
src/
├── lib/
│   └── ws-client.ts       — WSClient class (connect/disconnect/reconnect)
├── stores/
│   └── ws-store.ts         — Zustand store (snapshot + status)
└── features/dashboard/
    └── hooks/
        └── use-dashboard-ws.ts  — React hook (mount/unmount lifecycle)
```

## Risks / Trade-offs

- [Token race] → Token 30 秒有效，如果取得 token 後延遲連線可能過期。重連邏輯會重新取 token
- [多 tab] → 每個 tab 建立獨立 WS 連線。初期規模不是問題，未來可用 BroadcastChannel 共享
- [SSR] → WS 是純 client-side，`useDashboardWS` 須在 "use client" 元件中使用
