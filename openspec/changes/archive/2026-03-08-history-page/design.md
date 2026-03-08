## Context

後端 GET /executions 和 GET /billing 都使用 cursor-based pagination，回傳 `{ data: T[], pagination: { nextCursor?, hasMore } }`。前端需要用 TanStack Query 的 `useInfiniteQuery` 處理分頁，每次載入更多時帶 `after=nextCursor`。

## Goals / Non-Goals

**Goals:**
- Tab 切換 Executions / Billing 兩個表格
- Cursor-based 無限滾動（Load More 按鈕）
- 表格行顯示關鍵欄位 + 狀態 badge
- premium dark theme 表格樣式
- 空狀態處理

**Non-Goals:**
- 不實作 URL 同步 cursor（nuqs）— 簡化為記憶體狀態
- 不實作排序/篩選
- 不實作匯出功能

## Decisions

### D1: useInfiniteQuery + Load More

使用 TanStack Query `useInfiniteQuery`，`getNextPageParam` 從 `pagination.nextCursor` 取得。用 Load More 按鈕觸發 `fetchNextPage`，而非無限滾動 IntersectionObserver（簡單、可控）。

### D2: apiClient.getList 取得完整回應

因為分頁 API 回傳 `{ data: [], pagination: {} }` 不是 `{ data: T }` 格式，使用 `apiClient.getList<ExecutionListResponse>` 保留完整結構。

### D3: shadcn/ui Tabs 切換

用 shadcn/ui Tabs 在 Executions 和 Billing 之間切換，各自獨立查詢。Tab 切換時不重新 fetch（TanStack Query cache 保留）。

### D4: 表格用 div grid 而非 HTML table

用 CSS Grid `div` 實作表格，配合 premium dark theme（`bg-white/[0.02] border-white/5`）。更容易控制 responsive 和暗黑主題樣式。

### D5: 共用 LoadMoreButton

提取 Load More 按鈕為共用元件，接受 `hasMore`、`isFetchingNextPage`、`onClick` props。

## Risks / Trade-offs

- [大量資料效能] → 預設 limit=20，Load More 累積在記憶體中。極端情況下可考慮虛擬化，但目前不需要
- [Tab 狀態] → 切換 Tab 時兩個 query 都保留在 cache，不會重新 fetch（staleTime 預設）
