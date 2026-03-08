## Context

MDC 聚合器目前有 5 個信號源（book 25%, liquidation 20%, margin 20%, momentum 15%, crossccy 10%），預留 10% 給 intraday 信號。V10 §4.5 定義了日內時段效應，根據全球交易時段和月內位置產生 -0.5 ~ +0.5 的信號值。

現有信號模組遵循相同 pattern：實作 `Name() string` + `Compute(*domain.RawMarketData) domain.SignalValue`，由 `marketfeed.SignalSource` interface 消費。

## Goals / Non-Goals

**Goals:**
- 實作 Intraday signal，填補 MDC 預留的 10% 權重
- 遵循現有 signal 模組 pattern（純計算、零 I/O、只依賴 domain/）
- 三大高需求時段偵測 + 月內位置乘數

**Non-Goals:**
- 自適應校準（V10 §4.5 提到「每週根據績效數據自動更新」，需要歷史數據回饋機制，留待未來）
- 自訂時段配置（初版使用硬編碼時段，未來可透過 StrategyConfig 開放）

## Decisions

### 1. 時段定義使用 UTC 硬編碼

三個高需求時段（V10 §4.5）：
- 亞洲開盤：UTC 00:00–03:00
- 歐洲開盤：UTC 07:00–09:00
- 美國開盤：UTC 13:00–15:00

Bitfinex 全球用戶，用 UTC 是最合理的基準。不用 local timezone 避免 DST 問題。

**替代方案**：用 `RawMarketData.Timestamp` 的 local timezone → 拒絕，交易所行為以 UTC 為主。

### 2. 信號值平滑過渡，非階梯式

時段邊界不做硬切換（0 → +0.5），改用線性過渡區（前後 30 分鐘），避免信號跳變影響策略決策。

- 進入過渡區：0 → peak 線性上升
- 離開過渡區：peak → 0 線性下降
- 完全在時段內：peak 值
- 完全在時段外：0

**替代方案**：硬切換 → 拒絕，會造成時段邊界的利率突變。

### 3. 月內乘數作用於信號值本身

月內位置乘數（月末 ×1.1、月初 ×1.05、月中 ×1.0）直接乘以信號值，再 clamp 到 [-0.5, +0.5]。

這比在 MDC 層額外處理更簡單，且保持信號模組的自包含性。

### 4. 不需要歷史狀態

與 BookConsumption（需要 history buffer）不同，intraday signal 只看當前時間，是**無狀態**的。struct 可以是空的，不需要 buffer。

## Risks / Trade-offs

- **硬編碼時段可能不準**：實際需求高峰可能隨市場變化。→ 初版先用 V10 定義的時段，未來加自適應校準。
- **月內乘數會讓信號超出 ±0.5**：→ 用 clamp 強制限制在 [-0.5, +0.5]。
- **非高需求時段信號值為 0 而非負值**：V10 說低需求 -0.3~-0.5，但初版簡化為 0 表示中性。→ 如果需要，可以在非高需求時段加入小幅負值，但這會讓「中性」和「低需求」更難區分。採用 V10 規格：非高需求時段輸出 -0.3。
