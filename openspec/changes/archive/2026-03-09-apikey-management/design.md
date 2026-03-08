## Context

Dashboard 已有完整佈局（F5）和 Overview 首頁（F6），但目前使用者無法管理 API Key。API Key 是啟動放貸引擎的前置條件——使用者需要先新增 Bitfinex API Key、驗證權限後，引擎才能操作。後端已提供完整的 CRUD + verify API。

## Goals / Non-Goals

**Goals:**
- API Key 列表頁面，顯示所有已新增的 Key
- 新增 API Key 對話框（label + apiKey + apiSecret）
- 刪除確認對話框（防止誤刪）
- 一鍵驗證 Bitfinex 權限，顯示驗證狀態
- TanStack Query mutations（樂觀更新 query cache）
- premium dark theme 卡片風格

**Non-Goals:**
- 不實作 API Key 編輯（後端不支援 PUT）
- 不實作 API Secret 明文顯示（後端固定回傳 "****"）
- 不實作批次操作

## Decisions

### D1: 使用 Dialog 而非獨立頁面

新增/刪除用 shadcn/ui Dialog 實作，保持在同一頁面。API Key 操作頻率低（通常只設一把），不需要獨立的 create/edit 頁面。

### D2: Mutation 後 invalidate query cache

create/delete/verify mutation 成功後，用 `queryClient.invalidateQueries` 刷新列表，而非手動更新 cache。因為 API Key 操作頻率低，一次額外的 GET 請求可以接受。

### D3: 卡片式列表

每個 API Key 一張卡片（非表格），顯示：
- Label（名稱）
- API Key（masked，只顯示前 8 字元 + `...`）
- Exchange Status badge（verified → emerald, unverified → amber, failed → rose）
- Funding Balance（verified 時顯示餘額）
- 操作按鈕：Verify + Delete

### D4: 表單驗證

新增 API Key 表單用 Zod schema + react-hook-form：
- label: 必填，1-50 字元
- apiKey: 必填
- apiSecret: 必填

### D5: 空狀態

無 API Key 時，顯示引導提示 + 新增按鈕。

## Risks / Trade-offs

- [API Secret 安全] → 表單提交後不保留 secret 在前端；後端回傳永遠是 "****"
- [單一 API Key 限制] → 後端 UNIQUE constraint on user_id，新增第二把會回傳 409。前端在已有 key 時隱藏新增按鈕
