## Context

使用者完成註冊 + 綁定 Bitfinex API Key 後，需要設定放貸策略參數才能啟用自動放貸。目前已有 users、api_keys 資料表及對應的三層架構（handler → service → repository）。Strategy Config 是第三個 CRUD 資源，遵循完全相同的架構模式。

架構文件定義 StrategyConfig 包含 Signal/Execution/Period/Safety 四個子群組（對應 V10 附錄 B），但 MVP 階段放貸引擎尚未實作，只需支援基本的放貸參數。

## Goals / Non-Goals

**Goals:**
- 提供 strategy config CRUD API，讓使用者設定放貸策略
- 每個使用者一筆設定（與 API Key 的 one-per-user 一致）
- 參數驗證確保值在合理範圍內
- Config 資料以 JSONB 儲存，方便未來擴充參數而不需 migration

**Non-Goals:**
- 放貸引擎整合（ConfigReloader / WorkerManager）— 引擎尚未實作
- 完整 V10 附錄 B 全部參數 — MVP 只需基本放貸參數
- 即時通知 Worker 熱載入 — 未來引擎實作時再加

## Decisions

### 1. JSONB 儲存策略參數

**選擇**: user_configs 表使用 `config JSONB NOT NULL` 欄位儲存整個 StrategyConfig。

**替代方案**: 每個參數一個欄位（flat columns）。

**理由**: 策略參數會隨 V10 附錄 B 演進而擴充，JSONB 可以新增參數而不需要 DB migration。Go 端透過 `domain.StrategyConfig` struct 做型別安全的序列化/反序列化。驗證在 application layer 執行，不依賴 DB constraints。

### 2. MVP 參數範圍

**選擇**: MVP 只定義四組核心放貸參數：
- **Amount**: 放貸金額範圍（min/max）
- **Rate**: 日利率範圍（min/max）+ 自動計算的年利率參考
- **Period**: 放貸天數範圍（min/max，2-120 天）
- **AutoRenew**: 是否自動續借

**理由**: 這些是 Bitfinex Funding 最基本的掛單參數，使用者至少需要這些才能啟用放貸。進階參數（Signal、Strategy 相關）待引擎實作時擴充。

### 3. PUT upsert 語義

**選擇**: `PUT /configs` 使用 upsert 語義（INSERT ON CONFLICT UPDATE），不區分 create/update。

**替代方案**: 分開 POST（create）和 PUT（update）。

**理由**: 每個使用者只有一筆設定，前端不需要區分「首次建立」和「修改」。upsert 簡化了前端邏輯和 API 設計。成功回傳 200（含完整 config），不區分 201/200。

### 4. 刪除 = 重置為預設值

**選擇**: `DELETE /configs` 刪除整筆 config 記錄。

**理由**: 使用者可能想重新設定，刪除後再 PUT 新設定。不需要 soft delete。

## Risks / Trade-offs

- **[JSONB 無 DB level 驗證]** → Application layer 的 `Validate()` 方法負責驗證。所有寫入都經過 service layer，不會直接操作 DB。
- **[MVP 參數不完整]** → 預期中的取捨。StrategyConfig struct 設計為可擴充，未來新增子群組只需加 struct field + 更新 Validate()。
- **[無 lending engine 整合]** → Config 變更只持久化，不通知 Worker。未來加入 ConfigReloader interface 時是 additive change，不影響現有 CRUD。
