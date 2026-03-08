## Context

使用者設定放貸策略參數後，引擎才能依參數執行自動放貸。後端 PUT /configs 接收完整的 StrategyConfig JSON（currency, amount, rate, period, autoRenew），GET 回傳 UserConfig（包含 config 欄位），DELETE 重置為預設。目前只支援 USD 幣種。

## Goals / Non-Goals

**Goals:**
- 單一表單頁面，涵蓋所有策略參數
- 三區塊佈局：基礎設定（currency + autoRenew）、範圍參數（amount/rate/period 的 min-max）、操作按鈕
- react-hook-form + Zod 驗證（min ≤ max 跨欄位）
- 首次進入無設定時顯示預設值表單
- Save 成功後顯示 toast 或視覺回饋
- Reset to default 需確認

**Non-Goals:**
- 不實作三層切換（基礎滑桿/進階數值/專家 JSON）— 簡化為單一數值輸入表單，未來再加 slider
- 不實作 JSON 編輯模式
- 不安裝額外 UI 元件（用現有 Input + Button + Label）

## Decisions

### D1: 單一表單，不做三層切換

ROADMAP 提到「三層參數表單」，但 StrategyConfig 結構簡單（6 個數值 + 1 toggle），直接用數值輸入即可。滑桿和 JSON 模式是 nice-to-have，未來再加。

### D2: 預設值策略

首次進入（GET /configs 回 404）時，表單用 hardcoded 預設值填充：
- currency: "USD"
- amount: { min: 50, max: 10000 }
- rate: { min: 0.0001, max: 0.001 } (≈ 3.65% ~ 36.5% APR)
- period: { min: 2, max: 30 }
- autoRenew: true

使用者可直接儲存預設值或修改後儲存。

### D3: Zod 跨欄位驗證

使用 `.refine()` 確保每組 min ≤ max：
- amount.min ≤ amount.max
- rate.min ≤ rate.max
- period.min ≤ period.max

### D4: Rate 顯示為 APR

表單中利率以 APR (%) 顯示給使用者，內部轉換為日利率（÷ 365 ÷ 100）存入後端。讀取時反向轉換。

### D5: Save + Reset 操作

- Save: PUT /configs，成功後 invalidate configKeys.all
- Reset: DELETE /configs 後 invalidate，表單重填預設值
- Reset 前用 window.confirm 確認（不需額外 dialog 元件）

## Risks / Trade-offs

- [Rate 精度] → APR ↔ daily rate 轉換可能有浮點誤差，用 toFixed(4) 控制
- [404 處理] → GET /configs 無設定時回 404，hook 需把 404 視為「無設定」而非錯誤
