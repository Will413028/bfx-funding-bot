## Why

Overview 頁面目前透過 REST API + 60 秒 staleTime 拉取市場快照，使用者無法看到即時更新。F12 已建立 WebSocket 後端（Hub + ws-token），現在需要前端 WebSocket client 接收即時 MarketSnapshot 並更新 UI，完成即時推送的最後一哩路。

## What Changes

- 新增 `src/lib/ws-client.ts` — WebSocket client class，自動重連 + 指數退避 + heartbeat (pong)
- 新增 `src/stores/ws-store.ts` — Zustand store 管理 WS 連線狀態 + 最新 MarketSnapshot
- 新增 `src/features/dashboard/hooks/use-dashboard-ws.ts` — hook 整合 WS store，連線時自動取得 ws-token，將 snapshot 同步到 React Query cache
- 修改 Overview page — 整合 `useDashboardWS` hook，WS 連線時即時更新 MarketPanel

## Capabilities

### New Capabilities
- `ws-client`: 前端 WebSocket client（自動重連、指數退避、heartbeat、ws-token 認證）
- `ws-dashboard-integration`: Overview 頁面 WebSocket 整合（Zustand store + React Query cache sync）

### Modified Capabilities
_None — 新增 hook 不修改既有 REST 資料流，WS 為增量疊加_

## Impact

- **新增檔案**: `ws-client.ts`, `stores/ws-store.ts`, `hooks/use-dashboard-ws.ts`
- **修改檔案**: `overview/page.tsx`（加入 useDashboardWS hook）
- **依賴**: 使用原生 WebSocket API（不需額外套件）
- **環境變數**: 使用既有 `NEXT_PUBLIC_WS_URL`
