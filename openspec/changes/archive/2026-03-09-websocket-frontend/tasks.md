## 1. WebSocket Client

- [x] 1.1 Create `src/lib/ws-client.ts` — WSClient class with connect()/disconnect(), auto-reconnect (exponential backoff 1s→30s), onSnapshot callback, ws-token acquisition via apiClient

## 2. State Management

- [x] 2.1 Create `src/stores/ws-store.ts` — Zustand store with snapshot/status state, connect()/disconnect() actions that use WSClient
- [x] 2.2 Create `src/features/dashboard/hooks/use-dashboard-ws.ts` — React hook: mount→connect, unmount→disconnect, WS snapshot→queryClient.setQueryData for dashboard cache

## 3. Integration

- [x] 3.1 Update `src/app/[locale]/(dashboard)/overview/page.tsx` — add useDashboardWS() hook call, show WS connection status indicator

## 4. Verification

- [x] 4.1 Run `pnpm build` — verify production build succeeds
- [x] 4.2 Run `pnpm lint` — verify tsc + Biome pass
