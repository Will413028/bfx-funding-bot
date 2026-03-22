# Bitfinex 自動放貸 SaaS 平台：策略設計規範

> **最後更新**：2026-03-21
> **實作同步狀態**：本文件同時作為設計規範和實作參考。各章節標有 `⚡ 實作狀態` 的區塊說明實際程式碼與原始設計的差異。
> **實作程式碼**：`backend/internal/lending/` — signal/, strategy/, orderbook/, worker/, execution/
> **策略 Review**：`docs/strategy-journal.md` — 2026-03-21 全面 review 記錄

---

## 1. 系統核心目標

- **收益極大化**：透過複合信號決策框架（含信號時效性衰減與復甦平滑）統一調度動量追蹤、巨單避讓、週末溢價、事件日曆、供需面信號、日內時段效應、清算瀑布偵測與利率曲線形態分析，獲取遠超市場平均的利潤。
- **高成交率與全天候適應**：利用區間跳躍、隊首保留、深度感知拆分、排隊位置估算、部分成交管理與到期分散機制，確保牛熊市皆無資金閒置。
- **動態複利與高可用性**：具備事件驅動心跳、批次到期優化、利息即時再投資、優雅降級、冷啟動協議與指數退避容錯機制，確保系統 24/7 穩健運行。
- **策略可追蹤性**：內建績效追蹤模組，量化每個策略模組實際貢獻的 Alpha，便於持續調參。
- **主動風控與債權管理**：涵蓋已成交債權的全生命週期管理，從掛單、成交到到期歸還，全鏈路最佳化。
- **系統韌性**：具備優雅降級、閃崩保護、動態偏離保護、API 預算管理與冷啟動協議，確保任何單一故障不會癱瘓全局決策能力。
- **市場影響最小化**：具備自身市場衝擊管理與到期踩踏防護，確保資金規模成長後不會因自身操作擾動市場結構。
- **多租戶 SaaS 平台**：支援多用戶同時運行獨立策略實例，提供共享市場數據層、租戶隔離、API Key 安全管理、計費系統與自助管理儀表板。

---

## 2. 複合信號決策框架 (Composite Signal Scoring)

### 2.1 架構概述

所有市場信號源統一匯入**加權評分系統**，產出單一的**市場方向信心度 (Market Directional Confidence, MDC)** 分數，範圍 **-1.0（極度看跌）** 至 **+1.0（極度看漲）**。所有下游策略模組統一讀取 MDC 做決策。

### 2.2 信號源與權重

| 信號源 | 基礎權重 | 輸出範圍 | 延遲特性 | 設計 λ | 實作 λ |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Book 消耗速度（3.4） | **25%** | -1 ~ +1 | 最即時（秒級） | 0.05 | **0.01** |
| 清算瀑布（3.5） | **20%** | 0 ~ +1（僅看漲） | 即時（秒級） | 0.03 | **0.005** |
| 保證金持倉量（3.3） | **20%** | -1 ~ +1 | 中等（分鐘級） | 0.005 | **0.008** |
| 雙速 VWAP 動量（4.4） | **15%** | -1 ~ +1 | 中等（分鐘級） | 0.01 | 0.01 |
| 跨幣種 Funding Rate（3.6） | **10%** | -1 ~ +1 | 較慢（10-30 分鐘） | 0.002 | **0.005** |
| 日內時段效應（4.5） | **10%** | -0.5 ~ +0.5 | 預測性（小時級） | 0.001 | **0.002** |

> **⚡ 實作狀態** (`signal/mdc.go`)：基礎權重一致，λ 值偏差較大。實作的 λ 整體較低（信號衰減較慢），讓信號保持更長的影響力。待 G10 (Performance Tracking) 上線後以實際數據校準。

### 2.3 信號時效性衰減 (Signal Freshness Decay)

- **衰減公式**：`Effective_Weight_i = Base_Weight_i × e^(-λ_i × age_seconds_i)`
  - `age_seconds_i`：該信號最後一次成功更新距今的秒數。
  - `λ_i`：信號特定的衰減係數（見 2.2 表格）。
- **效果**：過時信號自然降低對 MDC 的影響力，決策品質平滑衰退而非突然崩潰。
- **標準化**：衰減後的權重歸一化，確保 `Σ Effective_Weight_i = 1.0`。

### 2.4 信號復甦平滑 (Signal Recovery Smoothing)

- **問題**：當信號因延遲已衰減至接近 0 權重，突然收到最新數據時，權重瞬間跳回滿額。若該數據恰好是極端值（如延遲期間累積的大量清算數據一次灌入），會造成 MDC 劇烈跳動。
- **平滑機制**：信號恢復後的前 3 個數據點，權重**線性遞增**回目標值：
  - 第 1 個數據點：恢復至目標權重的 **33%**。
  - 第 2 個數據點：恢復至 **66%**。
  - 第 3 個數據點：恢復至 **100%**。
- **例外**：清算瀑布信號因其硬性覆寫特性，不適用平滑機制——恢復後立即生效。

> **⚡ 實作狀態** (`signal/mdc.go`)：已實作（G2, `57d9b8c`）。Signal freshness decay 透過 `e^(-λ × age)` 實現。Recovery smoothing 透過 Confidence 因子間接達成——新出現的信號 Confidence 較低，隨樣本數遞增。

### 2.5 MDC 計算

- `MDC = tanh(Σ (Signal_i × Effective_Weight_i))`，壓縮至 [-1, +1]。
- **動態權重調整**：每週根據績效追蹤模組中各信號的歷史預測準確率，自動微調基礎權重（±10% 上限）。

> **⚡ 實作狀態** (`signal/mdc.go`)：`tanh` 壓縮已實作。G10 Performance Tracking 已完成（`6f19484`），提供 Confidence 因子。動態權重自動微調尚未實作（目前用固定 base weight）。公式：`Effective_Weight = Base_Weight × Confidence × e^(-λ × age)`。

### 2.6 MDC 驅動的策略映射

> **基準利率定義**：下表中的 `Rate` 統一定義為**當前 Order Book 最優可成交利率**（即 Dust Filter 過濾後，排隊最前方的最低利率掛單）。溢價係數以此為基礎計算。

| MDC 區間 | 市場判讀 | 溢價係數（設計） | 天數偏好 | 資金配置偏移 |
| :--- | :--- | :--- | :--- | :--- |
| +0.7 ~ +1.0 | 強烈看漲 | **×1.03** | 偏長天數 | 釣魚層加碼 |
| +0.3 ~ +0.7 | 溫和看漲 | **×1.02** | 標準 | 標準配置 |
| -0.3 ~ +0.3 | 中性 / 不確定 | **×1.00** | 偏短天數 | 前線加碼 |
| -0.7 ~ -0.3 | 溫和看跌 | `Rate - 1 Tick` | 2 天 | 全部前線 |
| -1.0 ~ -0.7 | 強烈看跌 | `Rate - 2 Ticks` | 2 天 | 全部前線，觸發機會成本評估 |

> **⚡ 實作狀態** (`strategy/pricing.go`)：P2（`03f63ad`）重寫為 **S1 BestAsk-Relative Pricing**：`rate = bestAsk - offset`，其中 `offset = 2.0 × minTickSize × mdcFactor × regimeFactor`。MDC ±1 對應 offset 0–2x，regime 調整（Contango 0.5x, Backwardation 2x, Crisis 0）。BestAsk 不可用時 fallback 回 FRR-based。同時整合 G15 Smart Wall Positioning：靠近 wall 時定價在 wall 下方 `minTickSize`。GT2 `maxPremiumDown` 已降至 0.18。

> **資金配置偏移說明**：此欄為相對於當前市場體制參數組（4.1）的**微調方向**，而非獨立的分層比例。例如「釣魚層加碼」代表在體制參數組的基礎上，將釣魚層比例提升 5–10%（從其他層扣除）；「前線加碼」代表將釣魚層資金轉移至第一層。具體偏移幅度見附錄 B。

### 2.7 信號衝突裁決

- 當個別信號與 MDC 總分方向相反時，以 MDC 為準。
- **唯一例外**：清算瀑布信號（3.5）為**硬性覆寫**——一旦觸發，強制 MDC = +1.0。

---

## 3. 掛單簿分析與市場感知 (Market Sensing)

### 3.1 動態雜訊過濾 (Dynamic Dust Filter)

根據市場體制（4.1）自動調整門檻：

| 市場體制 | Dust Filter 門檻 | 理由 |
| :--- | :--- | :--- |
| 趨勢牛市 | **500 USD** | 牛市小單為真雜訊 |
| 趨勢熊市 | **200 USD** | 熊市活躍度低，小單也有資訊價值 |
| 區間震盪 | **300 USD** | 中等過濾 |
| 危機模式 | **500 USD** | 危機時大量小額清算單是雜訊 |

> **⚡ 實作狀態** (`orderbook/dust.go`)：實作改用**中位數百分比**方式：`threshold = median(side_amounts) × 0.05`，不區分 regime。設計更自適應但失去 regime 差異化。

### 3.2 巨單牆偵測與領先掛單 (Wall Detection & Front-running)

- **單檔偵測**：前 30 檔有效掛單的中位數或 90% 分位數。絕對下限 `MIN_WALL_THRESHOLD = 50,000 USD`。若 Amount > max(Median × 5, MIN_WALL_THRESHOLD)，判定為「巨單牆」。
- **分散牆偵測**：滑動窗口計算連續 5 檔累積金額 > `MIN_WALL_THRESHOLD`，判定為「分散式阻力牆」。
- **動作**：目標位於牆後時，利率改為 `Wall_Rate - 0.00000001`。

> **⚡ 實作狀態** (`orderbook/wall.go` + `strategy/pricing.go`)：百分比門檻偵測：單檔 > 5% 同側總深度 → 單一牆；相鄰 entries（gap ≤ spread × 2）合計 > 5% → 分散牆。G15 Smart Wall Positioning 已完成（`03f63ad`）：靠近 wall 時定價在 wall 下方 `minTickSize`，搶先成交。

### 3.3 供需面信號追蹤 (Demand-Side Signal Tracking)

- **數據來源**：Bitfinex 公開的保證金多空持倉量。
- **變化率**：`ΔLong = (Long_now - Long_1h_ago) / Long_1h_ago`，`ΔShort` 同理。超過閾值（初始 5%）判定為需求暴增。
- **信號輸出**：標準化為 -1 ~ +1，輸入 MDC。

### 3.4 Order Book 消耗速度 (Book Consumption Velocity)

- **計算**：`Consumed = OB_total(t-N) - OB_total(t) + 期間新增掛單量`。
- **信號層級**：正常（±1σ）、加速（+2σ）、瘋搶（+3σ）。
- **信號輸出**：標準化為 -1 ~ +1，瘋搶輸出 +1.0。

### 3.5 清算瀑布偵測 (Liquidation Cascade Detection)

- **判定**：5 分鐘內累積清算金額突破動態閾值（初始 **5,000,000 USD**）。
- **動作**：硬性覆寫 MDC 為 +1.0，天數改為 **7–14 天**。
- **逐步回歸機制**：連續 15 分鐘無新增大額清算後，分 3 步退出硬性覆寫：
  - **第 1 個心跳**：解除硬性覆寫，但將清算瀑布信號的原始值從 +1.0 降至 **+0.7**，仍以正常權重輸入 MDC。
  - **第 2 個心跳**：信號值降至 **+0.3**。
  - **第 3 個心跳**：信號歸零，完全回歸正常 MDC 計算。
  - 若回歸期間再次偵測到大額清算，立即重新觸發硬性覆寫。
- **設計理由**：避免 MDC 從 +1.0 瞬間跳回正常值，與信號復甦平滑（2.4）的設計理念一致。

### 3.6 跨幣種相關性信號 (Cross-Currency Correlation)

- **追蹤**：BTC/ETH perpetual swap funding rate 的 5 分鐘變化率。
- **限制**：僅作為確認指標，基礎權重 10%。

### 3.7 競爭者行為偵測 (Competitor Tracking)

- 掛單後 **10 秒內**同一利率 ±2 ticks 出現 **3 筆以上**新掛單 → 假退再進（撤單 → 30 秒 → 更優利率重掛）。
- **冷卻**：同一心跳週期最多 1 次。

> **⚡ 實作狀態** (`orderbook/competitor.go`)：實作改用**統計偵測**：跨 10 個 snapshot 追蹤 `followRate`（新 entry 出現在 best ask 附近的頻率，權重 0.6）+ `roundRatio`（整數 bps 報價比例，權重 0.4）→ composite score [0, 1]。未實作假退再進的反制邏輯。

### 3.8 隱藏單深度估算 (Hidden Order Depth Estimation)

- **問題**：智慧隱藏掛單（4.3）根據「可見」競爭資金判斷是否使用隱藏單，但其他機器人的隱藏單不可見。若大量隱藏單存在，「競爭少」是假象，用公開單反而暴露意圖。
- **估算方法**：比較某利率的**可見掛單量**與該利率的**實際成交速度**。
  - `Hidden_Ratio = max(0, (Actual_Fill_Rate - Visible_Depth_Consume_Rate) / Actual_Fill_Rate)`
  - 若 `Hidden_Ratio > 30%`，代表有大量隱藏單存在。
- **影響**：當 `Hidden_Ratio > 30%` 時，4.3 的「競爭少」判斷自動打折——即使可見競爭資金少，仍傾向使用隱藏單。
- **數據累積**：Hidden_Ratio 以 30 分鐘滑動窗口統計，避免單筆大額成交造成誤判。

---

## 4. 利潤極大化與動態執行策略 (Yield Optimization & Execution)

### 4.1 市場體制識別 (Market Regime Classification)

| 體制 | 判定條件 | 天數偏好 | 溢價激進度 | 釣魚層比例 |
| :--- | :--- | :--- | :--- | :--- |
| **趨勢牛市** | MDC 持續 > +0.5 且長窗口 Sigma 高 | 偏長（14–30d） | 最激進 | 20–30% |
| **趨勢熊市** | MDC 持續 < -0.5 且長窗口 Sigma 高 | 全部 2d | 最保守 | 0% |
| **區間震盪** | MDC 在 ±0.3 間震盪且短窗口 Sigma 低 | 2–7d | 中性 | 0–10% |
| **危機模式** | 清算瀑布觸發 或 短窗口 Sigma > 歷史 +4σ | 7–14d | 最激進 | 20% |

- **體制參數組**：每種體制對應一組預設參數，切換時整組載入。

> **⚡ 實作狀態** (`signal/regime.go`)：
> - **命名差異**：設計「趨勢牛市/熊市」→ 實作 `Contango/Backwardation`（更符合放貸市場術語）。
> - **閾值差異**：進入門檻 ±0.3（非 ±0.5），退出門檻 ±0.2（hysteresis）。
> - **Crisis 判定**：`FlashFreeze || (|MDC| > 0.9 && volatility > 20%)`，未用 Sigma +4σ。
> - **未實作**：雙窗口 Sigma 判定、非對稱體制切換確認（1 vs 3 心跳）。
> - **額外實作**：Volatility EMA smoothing（α=0.3），DemandSupplyRatio 計算。

#### 非對稱體制切換確認

- **問題**：原設計要求連續 3 個心跳（活躍期 = 9 分鐘）確認才切換體制。對於「向上跳躍」場景（如熊市直接轉牛市），9 分鐘的延遲會錯過最初的利率飆升，而誤判的代價僅是多掛一些未成交的高利率單。
- **非對稱規則**：
  - **向上跳躍超過 1 個等級**（如熊市 → 牛市、震盪 → 危機）：確認期縮短至 **1 個心跳**。誤判代價低（未成交單會被 TTL 回收），延遲代價高（錯過暴漲）。
  - **同級或向下切換**（如牛市 → 震盪、震盪 → 熊市）：維持 **3 個心跳**確認。誤判代價高（過早降低激進度損失利潤），延遲代價低。
  - **任何體制 → 危機模式**：由清算瀑布硬性覆寫觸發，**0 延遲**立即生效。

### 4.2 動態資金分層佈署 (Dynamic Tier Allocation)

#### 雙窗口波動度判定

> **Sigma 高/低判定**：以各窗口過去 30 天的 Sigma 均值為基準。Sigma > 均值 × 1.5 判定為「高」，Sigma < 均值 × 0.7 判定為「低」，介於兩者之間依最近趨勢歸類。具體閾值見附錄 B.3。

| 短窗口 Sigma | 長窗口 Sigma | 判定 | 策略調整（流動位/市場位/釣魚位） |
| :--- | :--- | :--- | :--- |
| 高 | 高 | 趨勢性波動期 | 標準活躍模式（**40/40/20**） |
| 高 | 低 | 短期插針 | 插針模式（**30/40/30**） |
| 低 | 低 | 平穩死水期 | 標準死水模式（**60/40/0**） |
| 低 | 高 | 波動收斂期 | 過渡模式（**50/40/10**） |

- **體制覆寫**：4.1 的體制判定可覆寫上表的預設比例。

#### 釣魚層動態回收機制

- 連續 **N 個心跳**未成交（建議 N=5），50% 資金往前兩層挑撥。

#### 碎片清掃邏輯 (Dust Sweeping)

- 剩餘資金 < **52 USD** 時全數併入最後一層。

> **⚡ 實作狀態** (`strategy/allocation.go`)：實作改用**餘額門檻**分層，取代雙窗口波動度：
>
> | 餘額 | Tiers | 比例 | Rate 乘數 |
> | :--- | :--- | :--- | :--- |
> | < $150 | 1 tier | 100% core | ×1.0 |
> | $150–$1000 | 2 tiers | 70% core / 30% aggressive | ×1.0 / ×1.25 |
> | > $1000 | 3 tiers | 50% core / 30% moderate / 20% aggressive | ×1.0 / ×1.10 / ×1.25 |
>
> Regime cap：Crisis → max 1 tier，Backwardation → max 2 tiers。碎片清掃門檻 $50（Bitfinex 最低限額）。GT5 已將 aggressive tier 乘數從 1.25 降至 1.12。釣魚層回收機制和雙窗口波動度判定尚未實作（已知偏差，等波動度數據再做）。

### 4.3 智慧隱藏掛單機制 (Smart Hidden Offers)

- 單筆金額 **≥ 5,000 USD** 時判斷：
  - 競爭少且 Hidden_Ratio ≤ 30%（3.8）：**公開單**。
  - 競爭激烈，或 Hidden_Ratio > 30%：**隱藏單**（`flags: 64`）。

### 4.4 利率動量追蹤 (Rate Momentum)

- **快速 VWAP (5 分鐘)** / **慢速 VWAP (20 分鐘)**。
- **信號輸出**：標準化為 -1 ~ +1，輸入 MDC。
- **例外**：MDC ≥ +0.9 時忽略背離，強制追高。

### 4.5 日內時段效應 (Intraday Seasonality)

- **高需求時段**：亞洲開盤 UTC 00:00–03:00、歐洲開盤 UTC 07:00–09:00、美國開盤 UTC 13:00–15:00。
- **信號輸出**：高需求 +0.3~+0.5，低需求 -0.3~-0.5，輸入 MDC。
- **月內位置修正**：在溢價權重中加入月內位置乘數：
  - **月末（28–31 號）**：乘以 **1.1**（期貨結算、月末資金重配置需求上升）。
  - **月初（1–3 號）**：乘以 **1.05**（新月資金重新配置）。
  - **月中（4–27 號）**：乘以 **1.0**（基準）。
- **自適應校準**：每週根據績效數據自動更新時段與月內權重。

### 4.6 深度感知掛單拆分策略 (Depth-Aware Offer Splitting)

- 單層金額 **≥ 20,000 USD** 時：掃描附近 5 個 tick 深度，按 `Efficiency_k = Consume_Rate_k / (Depth_k + My_Amount_k)` 分配，每筆 ≥ 5,000 USD，上限 5 筆。
- **降級回退**：無法取得深度數據時均勻拆分。

### 4.7 心理價位避讓 (Psychological Tick Jumping)

- 活躍期避讓大關卡（0.05%, 0.1%, 0.15%, 0.2%），死水期額外避讓小關卡（0.02%, 0.03%）。
- 強制掛在 `整數 - 1 Tick`。

### 4.8 機會成本量化框架 (Opportunity Cost Framework)

- **期望值模型**：
  - `EV_deploy = current_rate × expected_duration_hours`
  - `EV_wait = P(higher_rate) × expected_higher_rate × expected_duration_hours - idle_hours × opportunity_cost_per_hour`
  - `P(higher_rate)` 根據 MDC 斜率與歷史統計估算。
  - `opportunity_cost_per_hour = 過去 7 天平均每小時利息收入 / 總資金`。
- **決策**：`EV_deploy > EV_wait` → 借出；反之待機（每心跳重算）。
- **整合**：取代利率地板的靜態 P10 判斷，P10 仍保留作為硬性下限兜底。

### 4.9 利率地板機制 (Rate Floor)

- `Rate_Floor = 過去 30 天 FRR 的 P10`。即使 EV 模型判定借出，低於 P10 仍不掛。
- **安全閥**：待機 > 2 小時，地板下調 20%。

> **⚡ 實作狀態** (`strategy/floor.go`)：三層 floor 取最大值：
> 1. 機會成本 floor：`Config.Rate.Min`
> 2. FRR 相對 floor：`EffectiveFRR × 0.92`（GT1: 0.80→0.92）
> 3. Regime 動態 floor：Crisis → `Rate.Min × 1.5`，Backwardation → `Rate.Min × 1.2`
>
> G11 Idle Capital Urgency 已實作（`e5825aa`）：`discount = min(idleMinutes/120, 0.15)`，idle > 120 分鐘最多下調 15%。未使用 P10 百分位（需歷史數據累積）。

### 4.10 閃崩保護 (Flash Crash Guard)

- **偵測**：利率 1 分鐘內下跌 > 50%。
- **動作**：暫停新增掛單 60 秒（不撤已有掛單）。
- **解除**：60 秒後回升至閃崩前 70% 以上恢復；否則地板機制接手。

> **⚡ 實作狀態** (`marketfeed/flashcrash.go`)：已實作。偵測閾值改為 dailyChangePerc ≤ -30%（非 1 分鐘 50%），凍結 cooldown 5 分鐘（非 60 秒），自動解除條件為 rate 回升（dailyChange > -30%）。FlashFreeze 旗標直接觸發 Crisis regime。

### 4.11 自身市場衝擊管理 (Self-Impact Management)

- `Impact_Ratio = 本機掛單金額 / 目標利率 ±3 ticks 總掛單量`。

| Impact_Ratio | 判定 | 動作 |
| :--- | :--- | :--- |
| < 10% | 低影響 | 正常操作 |
| 10%–25% | 中影響 | 強制隱藏單；撤重掛分批（間隔 5 秒） |
| > 25% | 高影響 | 隱藏單 + 分批；拆分上限降至 3 筆；撤掛交替（撤 1 掛 1） |

- **高影響模式**下，深度感知拆分偏向深度大的 tick。

### 4.12 排隊位置估算與主動降價 (Queue Position Estimation)

- `ETA = Queue_Ahead / Consume_Rate`。
- `ETA > TTL × 2` → 降價 1–2 Ticks；`ETA < TTL × 0.5` → 保留。
- 與 TTL 取先觸發者。

> **⚡ 實作狀態** (`strategy/queue.go`)：GT6（`03f63ad`）改用 **sigmoid smoothstep** 連續曲線取代兩段式跳躍。公式：`discount = 1.0 - 0.05 × smoothstep(queueRatio, 0.10, 0.60)`，其中 `smoothstep(x, e0, e1) = t² × (3 - 2t)`。深度比例方式（不需 Consume_Rate 數據）：
> - `queueDepth = AskDepth × (rate - BestAsk) / Spread`
> - `queueRatio > 0.50` → rate × 0.95（-5%）
> - `queueRatio > 0.20` → rate × 0.98（-2%）
> - Stale 偵測：`offer.Rate > BestAsk + 2×Spread` → 取消
>
> 2026-03-21 review 建議改用線性/sigmoid 曲線取代兩段式跳躍（見 ROADMAP GT6）。

### 4.13 部分成交管理 (Partial Fill Management)

- 殘餘利率偏離 > 5 Ticks → 撤銷重掛；< 52 USD → 併入下一筆；合理範圍 → 保留。

### 4.14 隨機擾動 (Anti-Frontrunning Noise)

- 最終利率加入 `0.00000001 ~ 0.00000005` 隨機值。

> **⚡ 實作狀態** (`strategy/noise.go`)：S3（`e5825aa`）**移除隨機擾動**，GT4 將 rate noise 從 ±1% 降至 ±0.004。目前只保留**心理價位避讓**：`psychStep = 0.0005`，靠近時偏移 `±0.00002`。無 amount noise。

---

## 5. 放貸天數決策邏輯 (Period Scaling)

### 5.1 利率曲線形態分析 (Term Structure Analysis)

| 曲線形態 | 特徵 | 天數策略 |
| :--- | :--- | :--- |
| **正常陡峭** | 長天期 > 短天期 | 套用 5.2 線性縮放 |
| **駝峰型** | 7–14d > 30d | 集中掛 **7–14 天** |
| **倒掛** | 短天期 > 長天期 | 全部 **2 天** |
| **平坦** | 各天期差異極小 | 全部 **2 天** |

- **判定**：`Slope = (Rate_30d - Rate_2d) / 28`，`Curvature = Rate_14d - (Rate_2d + Rate_30d) / 2`。
- **優先級**：作為天數決策的前置過濾器。與 4.1 市場體制取交集。

### 5.2 基於動態利差的線性天數縮放 (Dynamic Linear Scaling)

- `Spread = Rate_30d - Rate_2d`，`SMA_7d` 為動態基準。
- `Rate_30d < (SMA_7d * 1.2)` → 2 天；超過則線性遞增（最高 30 天）。
- 僅在曲線形態為「正常陡峭」時完整執行。

### 5.3 鎖倉機會成本折扣 (Lockup Opportunity Cost Discount)

- **問題**：掛了 30 天高息成交後，若市場轉為超級牛市利率翻倍，資金被鎖在舊利率中。提前歸還率修正（5.6）只考慮借方提前歸還，但「自己想收回卻不能」的機會成本未被量化。
- **折扣公式**：`Adjusted_Rate = Rate × (1 - lockup_discount × max(0, days - 2) / 28)`
  - 使用 `(days - 2)` 而非 `days`，確保最短的 2 天期貸款折扣為 0（鎖倉 2 天幾乎無機會成本）。30 天期折扣最大。
  - `lockup_discount` 根據當前波動度（短窗口 Sigma）動態調整：
    - 低波動（死水期）：`lockup_discount = 0.02`（鎖倉成本低，利率不太會大幅變化）。
    - 中波動（正常期）：`lockup_discount = 0.05`。
    - 高波動（活躍期 / 危機）：`lockup_discount = 0.10`（利率隨時可能翻倍，鎖倉代價高）。
- **效果**：高波動時系統自然傾向短天數快速周轉，最大化捕捉利率上行的機會；低波動時仍允許鎖定長天數穩定收益。
- **與曲線形態的協同**：折扣後的 Adjusted_Rate 作為 5.2 線性縮放和 5.6 有效回報計算的輸入，取代原始 Rate。

### 5.4 週末遞減溢價策略 (Cascading Weekend Premium)

- 建議以歷史回測優化啟動時間。預設 UTC 週四 12:00 至週五 18:00，門檻降 20%。
- 天數遞減：週五 14~30 天，週六 10~14 天，週日 2~7 天。

> **⚡ 實作狀態** (`strategy/weekend.go`)：S6（`03f63ad`）改為**動態週末溢價**：ComputeWeekendMultiplier 優先使用歷史 weekend/weekday ratio（若 > 1.0），否則 fallback 回固定值（Fri 18:00+ ×1.02, Sat ×1.05, Sun ×1.03）。未實作天數遞減邏輯。

### 5.5 特殊事件日曆 (Event Calendar Integration)

- 季度合約交割日、大型代幣解鎖、重大經濟數據（CPI、FOMC）。
- 事件前 **24–48 小時**主動拉長天數。

> **⚡ 實作狀態** (`strategy/calendar.go`)：實作為月末和季度結算的固定溢價（月末 ×1.02–1.05、季度 ×1.03–1.10，各 ±3 天窗口）+ 事件臨近時縮短 period。未整合外部事件源（代幣解鎖、CPI、FOMC）。

### 5.6 提前歸還率修正 (Early Return Adjustment)

- 各天數區間的歷史持有率 < 60% 則降低偏好權重。
- `有效回報 = Adjusted_Rate(5.3) × 持有率`。

> **⚡ 實作狀態** (`strategy/earlyreturn.go`)：已實作（G5, `57d9b8c`）。基於歷史提前歸還率計算 risk premium，調整 period 和 rate。

### 5.7 到期時間分散 (Maturity Staggering)

- **問題**：若大量資金在同一天以相同天數掛出，到期時會同時批次湧入 Order Book，造成供給瞬間暴增、壓低市場利率——本質上是對自己製造不利的供給衝擊（「到期踩踏」）。
- **機制**：在最終天數決策後，加入 **±1 天的隨機偏移**：
  - 目標 2 天 → 維持 2 天（最低天數不偏移）。
  - 目標 7 天 → 隨機分配為 6、7、8 天。
  - 目標 14 天 → 隨機分配為 13、14、15 天。
  - 目標 30 天 → 隨機分配為 29、30 天（Bitfinex 上限 30 天）。
- **分配比例**：同一心跳內的多筆掛單，盡量均勻分配到各偏移天數，避免偏向某一天。
- **效益**：到期時間自然分散，避免自身資金集中到期壓低利率，同時對個別掛單的利率影響極小（±1 天的利率差異通常可忽略）。

---

## 6. 系統架構與執行引擎 (System Architecture & Execution Engine)

### 6.1 事件驅動心跳 (Event-Driven Triggers)

- **定時心跳（兜底）**：活躍期 3 分鐘、死水期 15 分鐘。
- **事件心跳（即時）**：WebSocket 監聽清算瀑布啟動、Book 消耗瘋搶模式、利率閃崩、大額成交（>100,000 USD）。
- **防抖**：觸發後 **30 秒**內不接受新觸發。
- **與定時心跳的關係**：事件心跳觸發後重置定時計時器。

### 6.2 API 預算管理 (API Budget Management)

> **多租戶模式**：本節的 P0/P1/P2 優先級邏輯在 Worker 內部仍然生效，但每個 Worker 可用的總配額由全局 API 配額分配器（§10.5）動態決定，而非固定值。共享層的公開數據拉取由共享市場數據服務（§10.2）統一處理，不計入 Worker 配額。

| 優先級 | 數據類型 | 拉取頻率 | 歸屬 |
| :--- | :--- | :--- | :--- |
| **P0 核心** | Order Book、錢包餘額、活躍掛單 | 每次心跳必拉 | 共享層(OB) / Worker(錢包、掛單) |
| **P1 重要** | 成交流、清算流 | WebSocket 持續監聽 | 共享層 |
| **P2 輔助** | 持倉量、跨幣種 Funding Rate | 每 2–3 個心跳（死水期每 5 個心跳） | 共享層 |

- **操作預算保留**：全局配額的 **40%** 預留給所有 Worker 的掛撤單操作（§10.5）。
- **超支處理**：犧牲 P2，使用快取值。

### 6.3 優雅降級機制 (Graceful Degradation)

- **健康度**：健康（2 心跳內更新）、警告（>2 心跳）、故障（>5 心跳）。

| 故障數據源 | 降級策略 |
| :--- | :--- |
| 清算資料流 | Book 消耗速度權重提升至 35% |
| 持倉量 API | 回退至 VWAP + Book 消耗雙信號 |
| 跨幣種 Funding Rate | 移除信號，其餘權重等比放大 |
| Order Book | **最高級警報**：純 FRR 跟隨模式 |
| 成交流 | VWAP 凍結，降級至持倉量 + 跨幣種信號 |

- **恢復機制**：恢復後經 2 個心跳穩定確認才重新納入（信號復甦平滑 2.4 同步生效）。

### 6.4 冷啟動協議 (Cold Start Protocol)

- **冷啟動階段**（前 30 分鐘或直到所有信號源歷史窗口填滿）：FRR 跟隨、2 天短單、死水期配置、MDC = 0.0。
- **逐步啟用**：
  - 0–10 分鐘：純 FRR，累積數據。
  - 10–20 分鐘：啟用 VWAP，MDC 權重 50%。
  - 20–30 分鐘：啟用 Book 消耗、供需信號，MDC 權重 75%。
  - 30 分鐘後：完整策略模式。
- **宕機恢復**：冷啟動期間仍執行債權管理（7.2），確保到期資金不閒置。

---

## 7. 複利與維護流程 (Maintenance)

### 7.1 動態複利與重掛循環 (Dynamic Compounding Cycle)

#### 動態心跳機制

- 活躍期 3 分鐘、死水期 15 分鐘定時心跳 + 事件心跳（6.1）。

#### 隊首保留檢查 (Queue Retention)

- 誤差 `< 0.00000002` 且天數相同則保留。

#### 僵屍單動態超時撤銷 (Dynamic Stale Order TTL)

- 活躍期 10–15 分鐘 / 死水期 45–60 分鐘。與排隊位置估算（4.12）取先觸發者。

#### 原子化換單機制 (Atomic Order Replacement)

- 餘額允許時先掛新單再撤舊單；否則回退傳統流程。

#### 利息即時再投資 (Interest Reinvestment Sweep)

- 每心跳檢查錢包，可用餘額 > 52 USD（含新結算利息）立即納入掛單資金池。
- 利息 < 52 USD 暫不處理，等待累積或與到期資金合併。

### 7.2 已成交債權管理 (Active Credit Management)

#### Auto-Renew 策略

- 永遠關閉。API 連續異常超過 3 次退避或網路中斷時臨時開啟，恢復後立即關閉。

#### 債權到期預排程

- 到期前 1 個心跳週期預先計算最佳掛單參數。

#### 批次到期優化 (Credit Batch Optimization)

- 5 分鐘匯集窗口，2 筆以上到期時統一分層佈署。
- **與到期分散的協同（5.7）**：因初始掛單已加入 ±1 天偏移，批次到期的資金量自然更均勻，減輕單次批次的市場衝擊。

---

## 8. 安全與例外保護 (Safety Guards)

### 8.1 動態市場偏離保護 (Adaptive Deviation Guard)

| 市場體制 | 偏離閾值 | 理由 |
| :--- | :--- | :--- |
| 趨勢牛市 | **40%** | 牛市偏離合理擴大 |
| 趨勢熊市 | **20%** | 緊跟 FRR |
| 區間震盪 | **25%** | 標準保護 |
| 危機模式 | **60%** | 捕捉極端行情暴利 |

- **硬性上限**：偏離度不超過 **80%**。

### 8.2 系統與 API 基礎安全

- **權限控制**：API Key 嚴格禁用 Withdrawal，僅限 Read/Write Funding。
- **IP 限制**：Bitfinex IP Whitelisting。
- **防呆機制**：餘額 < 50 USD 自動待機。
- **Rate Limit & Nonce**：內建請求延時與全域 Nonce 生成器。

### 8.3 網路與 API 容錯機制 (Fault Tolerance)

- 指數退避：(2^c - 1) 秒 + 隨機延遲，最多 5 次。
- 連續退避達上限 → 防禦性 auto-renew（7.2）+ 優雅降級（6.3）。

---

## 9. 績效追蹤模組 (Performance Tracking)

### 9.1 成交紀錄與 Alpha 量化

- **每筆成交紀錄**：`user_id`、實際成交利率、當時 FRR、當時 VWAP、當時 MDC 分數、當時市場體制、掛單層別、是否隱藏單、掛單天數（含偏移）、等待時間、觸發來源（定時 / 事件心跳）、觸發策略標籤、自身市場衝擊比率、信號平均鮮度、Hidden_Ratio。
- **Alpha 計算**：`Alpha = 實際成交利率 - 當時 FRR`。

### 9.2 策略模組歸因

分別追蹤以下策略的獨立效果：

- 動量追蹤溢價 vs 標準掛單的利率差異
- 巨單避讓的成交速度提升
- 週末溢價策略的實際收益增量
- 事件日曆觸發的天數鎖定效果
- 供需面信號的領先預判準確率
- 日內時段溢價的邊際收益
- 月內位置修正的邊際收益
- 深度感知拆分 vs 均勻拆分的成交率差異
- 各天數區間的實際持有率與有效回報
- Book 消耗速度信號的追高成功率
- 清算瀑布偵測的額外收益捕捉量
- 排隊位置估算觸發的成交時間改善
- 部分成交殘單重掛的利率改善
- 利率曲線形態判斷的天數選擇準確度
- 批次到期 vs 逐筆處理的資金利用率差異
- MDC 分數與實際利率走勢的相關性
- 事件心跳 vs 定時心跳的反應時間與收益差異
- 各市場體制下的平均 Alpha
- 降級模式下的決策品質衰減幅度
- 閃崩保護觸發的損失避免金額
- 機會成本模型 EV_wait 觸發的待機收益 vs 損失
- 自身市場衝擊管理觸發的頻率與成交品質影響
- 信號衰減權重 vs 固定權重的 MDC 預測精度差異
- 信號復甦平滑 vs 瞬間恢復的 MDC 穩定性比較
- 利息即時再投資的累計複利增量
- 冷啟動階段 vs 正常模式的 Alpha 差異
- 動態 Dust Filter 在不同體制下的信號品質影響
- 鎖倉折扣調整後的天數分佈 vs 原始天數分佈的收益差異
- 到期分散後的批次衝擊 vs 未分散的批次衝擊
- 非對稱體制切換的觸發頻率與利率捕捉改善
- 隱藏單深度估算對公開/隱藏單選擇的修正效果

### 9.3 自適應參數回饋 (Adaptive Parameter Feedback)

- **每週自動微調參數**：
  - 複合信號各信號源基礎權重（2.2）
  - 信號衰減係數 λ（2.3）
  - 信號復甦平滑的遞增步數（2.4）
  - 市場體制切換閾值（4.1）
  - 非對稱切換的跳躍判定閾值（4.1）
  - 日內時段溢價權重（4.5）
  - 月內位置修正乘數（4.5）
  - 深度感知拆分的 Efficiency 計算權重（4.6）
  - 機會成本模型中的 P(higher_rate) 估算參數（4.8）
  - 自身衝擊比率分級閾值（4.11）
  - 鎖倉機會成本折扣的各波動度 lockup_discount（5.3）
  - 到期分散的偏移天數範圍（5.7）
  - 週末溢價啟動時間（5.4）
  - 天數區間偏好權重（5.6）
  - 供需信號閾值（3.3）
  - Book 消耗速度的 σ 閾值（3.4）
  - 清算瀑布的金額閾值（3.5）
  - Hidden_Ratio 判定閾值（3.8）
  - 排隊位置 ETA 倍數閾值（4.12）
  - 曲線形態的斜率與曲率閾值（5.1）
  - 各市場體制的偏離保護閾值（8.1）
  - 事件心跳的防抖間隔（6.1）
  - 各市場體制的 Dust Filter 門檻（3.1）
- **約束**：單次調整幅度不超過原值的 **±10%**。
- **輸出**：每日**每用戶**摘要報告（總 APY、各策略 Alpha、資金利用率、平均等待時間、持有率統計、曲線形態分佈、MDC 預測準確率、體制分佈、降級事件記錄、信號鮮度統計、市場衝擊事件記錄、冷啟動事件記錄、到期分散效果、鎖倉折扣效果）+ 系統層級彙總報告。

---

## 10. 多租戶 SaaS 平台架構 (Multi-Tenant SaaS Architecture)

### 10.1 架構概述

系統分為**共享層**（市場數據與信號計算，所有用戶共用）和**私有層**（每用戶獨立的策略執行與掛單操作），以最小化資源消耗並確保租戶隔離。

```
┌──────────────────────────────────────────────────────────┐
│                   Frontend (Next.js)                      │
│   用戶註冊/登入 ─ API Key 管理 ─ 績效儀表板 ─ 付費管理  │
│   策略參數自訂 ─ 即時狀態監控 ─ 歷史報表                 │
└─────────────────────────┬────────────────────────────────┘
                          │ HTTPS / WebSocket
┌─────────────────────────▼────────────────────────────────┐
│                   API Gateway (Go)                        │
│   JWT 認證 ─ 用戶 CRUD ─ 計費 ─ 策略配置 ─ Rate Limit   │
└─────────────────────────┬────────────────────────────────┘
                          │ Internal gRPC / Channel
┌─────────────────────────▼────────────────────────────────┐
│                 Strategy Engine (Go)                       │
│                                                           │
│  ┌─────────────────────────────────────────┐              │
│  │     Shared Market Data Service          │              │
│  │  1 組公開 WS 連接 → 全局信號計算        │              │
│  │  Order Book, 成交流, 清算流, FRR        │              │
│  │  持倉量, 跨幣種 Funding Rate            │              │
│  │  ↓ 輸出：MDC, 市場體制, 利率曲線形態   │              │
│  └──────────────────┬──────────────────────┘              │
│                     │ broadcast (Go channel / Redis Pub)   │
│  ┌──────────────────▼──────────────────────┐              │
│  │     Per-User Worker Pool                │              │
│  │                                          │              │
│  │  Worker[User_A] ─ 私有 WS 連接          │              │
│  │    ├─ 讀取共享 MDC + 體制               │              │
│  │    ├─ Phase 4-5: 利率/天數決策 + 掛單   │              │
│  │    └─ 用戶專屬參數配置                  │              │
│  │                                          │              │
│  │  Worker[User_B] ─ 私有 WS 連接          │              │
│  │  Worker[User_C] ─ ...                   │              │
│  └──────────────────────────────────────────┘              │
│                                                           │
│  ┌─────────────────────────────────────────┐              │
│  │     Global API Quota Allocator          │              │
│  │  所有用戶共享 Bitfinex IP 級 Rate Limit │              │
│  │  動態分配每 Worker 的操作預算           │              │
│  └─────────────────────────────────────────┘              │
└─────────────────────────┬────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────┐
│  PostgreSQL ─ Redis ─ KMS                                 │
│  users, api_keys(加密), configs(per-user),                │
│  executions(user_id), billing, sessions                   │
└──────────────────────────────────────────────────────────┘
```

### 10.2 共享市場數據服務 (Shared Market Data Service)

- **職責**：執行 V10 流水線的 **Phase 0–3**（數據拉取、信號計算、MDC 計算、體制判定），所有用戶共用同一份結果。
- **輸出**：每次心跳產出一個 `MarketSnapshot` 結構，包含：
  - MDC 分數、當前市場體制、體制參數組
  - 各信號源原始值與 Effective_Weight
  - Order Book 快照（含 Dust Filter 過濾後結果）
  - 巨單牆位置、Hidden_Ratio
  - 利率曲線形態判定結果
  - 閃崩凍結狀態
- **廣播機制**：`MarketSnapshot` 透過 Go channel（單機）或 Redis Pub/Sub（多機）廣播給所有活躍 Worker。
- **資源消耗**：無論 1 個用戶或 1,000 個用戶，共享層的 API 呼叫量和計算量不變——1 組公開 WebSocket 連接 + Phase 0–3 的計算。

### 10.3 Per-User Worker 設計

#### Worker 職責

每個付費用戶對應一個獨立的 Worker goroutine，負責：

- 接收共享層廣播的 `MarketSnapshot`
- 載入該用戶的**個人化策略參數**（附錄 B 的所有參數均為 per-user 可配置）
- 執行 **Phase 4–5**：利率/天數決策 + 掛單執行
- 透過該用戶的私有 WebSocket 連接操作 Bitfinex 帳戶
- 執行 Phase 6–7：Auto-Renew 管理 + 紀錄寫入（帶 `user_id`）

#### Worker 生命週期

| 事件 | 動作 |
| :--- | :--- |
| 用戶付費啟用 | Spawn Worker goroutine → 解密 API Key → 建立私有 WS 連接 → 進入冷啟動（6.4） |
| 正常運行 | 每次共享層廣播 MarketSnapshot 時執行 Phase 4-7 |
| 用戶手動暫停 | Graceful shutdown → 撤銷所有未成交掛單 → 關閉私有 WS → Worker 休眠 |
| 用戶付費到期 | 同暫停，另加開啟防禦性 auto-renew 兜底 → 標記帳戶為停用 |
| Worker 崩潰 | 自動重啟 → 帶冷啟動協議 → 錯誤日誌寫入 → 若連續崩潰 3 次，暫停並通知用戶 |
| API Key 失效 | 偵測到認證錯誤 → 暫停 Worker → 通知用戶更新 Key |

#### Worker 隔離保證

- **錯誤隔離**：Worker A 崩潰不影響 Worker B（獨立 goroutine + recover）。
- **數據隔離**：每個 Worker 只能存取自己的 `user_id` 數據（PostgreSQL Row-Level Security）。
- **資源隔離**：每個 Worker 有獨立的記憶體中狀態（VWAP 窗口、TTL 計時器、排隊位置快取等），不與其他 Worker 共享可變狀態。

### 10.4 API Key 安全管理

#### 加密存儲

- **加密算法**：AES-256-GCM（認證加密，防篡改）。
- **加密金鑰管理**：
  - 生產環境：使用雲端 KMS（AWS KMS / GCP Cloud KMS / HashiCorp Vault）。
  - 加密金鑰**永不進入資料庫**，僅存在環境變數或 KMS 中。
- **解密時機**：僅在 Worker 啟動時解密到記憶體，記憶體中以 `[]byte` 持有，Worker 停止時清零。
- **資料庫儲存格式**：`encrypted_api_key | encrypted_api_secret | nonce | tag`（全部 Base64 編碼）。

#### 權限驗證

- **入庫前驗證**：用戶提交 API Key 後，系統立即用該 Key 呼叫 Bitfinex 的 `key_permissions` endpoint，驗證：
  - ✅ 必須開啟：Read, Write (Funding)
  - ❌ 必須關閉：Withdrawal
  - 若 Withdrawal 權限開啟，**拒絕儲存**，要求用戶重新設定。
- **定期覆核**：每 24 小時重新驗證一次 Key 權限，若發現異常立即暫停 Worker 並通知。

#### 安全審計

- 所有 API Key 的建立、讀取（解密）、刪除操作寫入獨立的 `audit_log` 表。
- 支援用戶隨時撤銷並更換 API Key（觸發 Worker 重啟）。

### 10.5 全局 API 配額分配器 (Global API Quota Allocator)

- **問題**：V10 §6.2 的 API 預算管理是 per-user 設計，但 SaaS 場景下所有用戶共享同一個 IP 的 Bitfinex rate limit。N 個用戶各自獨立管理預算可能導致全局超支。
- **架構**：引入一個全局配額分配器，取代 per-user 的 §6.2 邏輯。

#### 配額池設計

```
Bitfinex IP 級 Rate Limit（每分鐘 R 次請求）
│
├─ 共享層預留：固定 15%（公開數據拉取，不隨用戶數變化）
├─ 操作預留池：固定 40%（所有用戶的掛撤單操作）
└─ Per-User 數據池：剩餘 45%（各 Worker 的私有數據拉取）
    │
    └─ 均分給 N 個活躍 Worker：每 Worker 分配 45% / N
```

#### 動態調整

- **活躍 Worker 少**：每 Worker 配額充裕，可拉取更多私有數據（如更頻繁的錢包餘額查詢）。
- **活躍 Worker 多**：每 Worker 配額壓縮，私有數據拉取降頻，但操作預留池不受影響（確保每個用戶的掛撤單能力）。
- **操作預留池的內部排程**：當多個 Worker 在同一心跳需要操作時，按**先到先服務 + 公平輪轉**分配，確保沒有 Worker 長期飢餓。

#### 與 V10 §6.2 的關係

原 §6.2 的 P0/P1/P2 優先級邏輯仍在 Worker 內部生效，但每個 Worker 可用的總配額由全局分配器動態決定，而非固定值。

### 10.6 用戶策略參數管理

- **預設模板**：新用戶啟用時，載入附錄 B 的全部預設參數作為初始配置。
- **自訂層級**：

| 層級 | 可調整項目 | UI 呈現 |
| :--- | :--- | :--- |
| **基礎** | 利率地板開關、天數上限、釣魚層開關 | 簡單開關 + 滑桿 |
| **進階** | 溢價係數倍率、Dust Filter 門檻、TTL 範圍、鎖倉折扣 | 數值輸入 + 合理範圍提示 |
| **專家** | 附錄 B 的所有參數 | 完整參數表 + JSON 編輯器 |

- **儲存**：每用戶的參數覆寫存為 PostgreSQL 的 JSONB 欄位，Worker 啟動時合併 `default_config ← user_overrides`。
- **即時生效**：用戶修改參數後，透過 API Gateway 通知對應 Worker，Worker 在下一個心跳載入新配置（不需重啟）。
- **自適應回饋隔離**：§9.3 的每週自動微調以 per-user 為單位執行——用戶 A 的歷史數據只調整用戶 A 的參數。

### 10.7 計費系統 (Billing)

#### 計費模型選項

| 模型 | 計算方式 | 優點 | 缺點 |
| :--- | :--- | :--- | :--- |
| **固定月費** | 按方案收費（如 $29/$99/$299） | 收入可預測、實作簡單 | 小額用戶覺得貴、大額用戶覺得便宜 |
| **AUM 抽成** | 管理資金的 X% / 年 | 與用戶利益對齊 | 需要精確追蹤資金量 |
| **利潤分潤** | 機器人產出利潤的 X% | 用戶最容易接受 | 需定義「利潤」基準、實作複雜 |
| **混合** | 低月費 + 利潤分潤 | 平衡各方 | 計費邏輯最複雜 |

#### 建議：混合模型

- **基礎月費**（覆蓋平台運營成本）+ **超額利潤分潤**（激勵平台持續優化策略）。
- 利潤定義：`用戶期間利息收入 - 同期 FRR 被動收入`（即機器人的 Alpha 部分）。
- 計費週期：每月結算，從用戶 Bitfinex 帳戶的利息收入中扣除（需用戶事先授權）或發送帳單。

#### 計費數據追蹤

- 每個 Worker 在 §9.1 的成交紀錄中已包含 `user_id`、實際利率、FRR、Alpha。
- 月末由計費服務聚合 `SUM(Alpha × Amount × Days)` 計算分潤。
- 計費相關的所有計算邏輯存放在獨立的 `billing_service` 中，與策略引擎解耦。

### 10.8 前端儀表板 (Dashboard)

#### 核心頁面

| 頁面 | 功能 |
| :--- | :--- |
| **首頁/概覽** | 當前 AUM、今日/本月 APY、活躍債權數、即時 MDC 與體制狀態 |
| **績效報表** | 歷史 APY 曲線、各策略 Alpha 歸因圖表、天數分佈、利率分佈 |
| **活躍掛單** | 即時掛單清單、各層位分佈、排隊位置估算、部分成交狀態 |
| **債權管理** | Active credits 列表、到期時間軸、提前歸還率統計 |
| **策略配置** | 基礎/進階/專家三層參數調整 |
| **API Key 管理** | 新增/更換/刪除 Key、權限狀態顯示、最後驗證時間 |
| **計費帳單** | 本月費用明細、歷史帳單、付費方式管理 |

#### 即時數據推送

- Dashboard 透過 WebSocket 連接 API Gateway，接收用戶所屬 Worker 的即時狀態更新。
- 推送內容：掛單成交通知、體制切換通知、異常警報（API Key 失效、Worker 崩潰、閃崩觸發等）。

### 10.9 監控與告警 (Observability)

#### 系統層級監控（運營團隊）

| 指標 | 工具 | 告警條件 |
| :--- | :--- | :--- |
| 活躍 Worker 數 | Prometheus + Grafana | Worker 數驟降 > 10% |
| 全局 API 配額使用率 | Prometheus | 使用率持續 > 85% |
| 共享層心跳延遲 | Prometheus | 延遲 > 心跳週期的 50% |
| Worker 崩潰率 | Prometheus | 任一 Worker 1 小時內崩潰 > 3 次 |
| PostgreSQL 連線池 | Prometheus | 使用率 > 80% |
| WebSocket 連接健康度 | 自定義 | 任一連接斷線 > 2 分鐘未重連 |

#### 用戶層級監控（自動通知用戶）

| 事件 | 通知方式 | 說明 |
| :--- | :--- | :--- |
| API Key 失效 | Email + Dashboard 警報 | 要求用戶更新 Key |
| Worker 連續崩潰 | Email + Dashboard | 暫停策略，等待人工介入 |
| 錢包餘額 < 50 USD | Dashboard | 策略進入待機模式 |
| 單日 Alpha < 0（跑輸 FRR） | Dashboard | 僅提示，不暫停 |
| 付費即將到期 | Email（提前 7 天） | 續費提醒 |

### 10.10 資料庫 Schema 概述

```sql
-- 用戶
users (
    id UUID PK,
    email VARCHAR UNIQUE,
    password_hash VARCHAR,
    plan_tier VARCHAR,          -- 'basic' / 'advanced' / 'expert'
    status VARCHAR,             -- 'active' / 'suspended' / 'expired'
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)

-- API Key（加密儲存）
api_keys (
    id UUID PK,
    user_id UUID FK → users,
    encrypted_key BYTEA,        -- AES-256-GCM 加密
    encrypted_secret BYTEA,
    nonce BYTEA,
    permissions_verified_at TIMESTAMP,
    status VARCHAR,             -- 'active' / 'invalid' / 'revoked'
    created_at TIMESTAMP
)

-- 用戶策略參數覆寫
user_configs (
    user_id UUID PK FK → users,
    config_overrides JSONB,     -- 僅存與預設不同的參數
    updated_at TIMESTAMP
)

-- 成交紀錄（V10 §9.1 + user_id）
executions (
    id BIGSERIAL PK,
    user_id UUID FK → users,
    actual_rate DECIMAL,
    frr_at_time DECIMAL,
    vwap_at_time DECIMAL,
    mdc_score DECIMAL,
    market_regime VARCHAR,
    tier VARCHAR,
    is_hidden BOOLEAN,
    period_days INTEGER,
    wait_seconds INTEGER,
    trigger_source VARCHAR,     -- 'scheduled' / 'event'
    strategy_tags TEXT[],
    impact_ratio DECIMAL,
    signal_freshness DECIMAL,
    hidden_ratio DECIMAL,
    executed_at TIMESTAMP
)
-- 索引：(user_id, executed_at) 用於績效查詢
-- RLS Policy：用戶只能查看自己的 executions

-- 計費紀錄
billing_records (
    id BIGSERIAL PK,
    user_id UUID FK → users,
    period_start DATE,
    period_end DATE,
    base_fee DECIMAL,
    alpha_profit DECIMAL,
    profit_share DECIMAL,
    total_charge DECIMAL,
    status VARCHAR,             -- 'pending' / 'paid' / 'overdue'
    created_at TIMESTAMP
)

-- 審計日誌
audit_log (
    id BIGSERIAL PK,
    user_id UUID,
    action VARCHAR,             -- 'api_key_created' / 'api_key_decrypted' / 'config_updated'
    details JSONB,
    ip_address INET,
    created_at TIMESTAMP
)

-- Worker 狀態快照（用於監控）
worker_snapshots (
    user_id UUID FK → users,
    status VARCHAR,             -- 'running' / 'paused' / 'crashed' / 'cold_start'
    last_heartbeat_at TIMESTAMP,
    active_offers_count INTEGER,
    active_credits_count INTEGER,
    wallet_balance DECIMAL,
    current_regime VARCHAR,
    updated_at TIMESTAMP
)
```

### 10.11 部署架構

#### 初期（< 100 用戶）

- **單機部署**：Docker Compose 管理所有服務（API Gateway + Strategy Engine + PostgreSQL + Redis）。
- Go 的 goroutine 模型讓 100 個 Worker 在單台 4 核 8GB 機器上就能穩定運行。
- 共享層 + 100 個 Worker 的記憶體估算：約 2-3 GB。

#### 中期（100–1,000 用戶）

- **拆分部署**：Strategy Engine 獨立為 1-2 台機器，API Gateway + Frontend 獨立一台，PostgreSQL 獨立一台。
- 共享層的 `MarketSnapshot` 改為 Redis Pub/Sub 廣播，支援 Strategy Engine 多機水平擴展。
- 每台 Strategy Engine 機器承載 300-500 個 Worker。

#### 遠期（> 1,000 用戶）

- Kubernetes 編排，Strategy Engine 以 Pod 為單位水平擴展。
- PostgreSQL 讀寫分離（寫入主庫，績效查詢走讀副本）。
- 引入 Worker 排程器，根據用戶的 AUM 和活躍度分配到不同的 Engine 節點。

---

## 11. 2026-03-21 策略 Review 新增項目

> 基於全面策略 review 新增的收益優化項目。完整分析見 `docs/strategy-journal.md`。

### 11.1 策略 Pipeline 接線 (G0)

**問題**：`factory.go` 目前只實例化 `PricingStrategy`，其餘 12 個策略模組寫好但未組合使用。

**方案**：建立 `CompositeStrategy`，依序執行 13 個模組，各模組的 `DecisionResult` 合併後交由 Executor 執行。

### 11.2 閒置資金急迫度 (G11)

**問題**：引擎不追蹤資金閒置時長，死資金的 APY = 0%。

**方案**：
- 追蹤 `lastLentAt` 時間戳
- `urgencyDiscount = min(idleMinutes / 120, 0.15)`
- `effectiveFloor = floor × (1 - urgencyDiscount)`

### 11.3 FRR 趨勢追蹤 (G12)

**問題**：只用 spot FRR 定價，不知道 FRR 是在上升還是下降趨勢。

**方案**：
- `frrEMA_short = EMA(FRR, 30min)`
- `frrEMA_long = EMA(FRR, 4hr)`
- `frrTrend = (short - long) / long`
- 上升趨勢 → pricing 更積極，下降趨勢 → 搶先成交

### 11.4 歷史 Fill Rate 學習 (G13)

**問題**：所有 premium/discount 常數缺乏回饋迴路。

**方案**：追蹤每個 rate bucket 的 fill rate + time-to-fill，用數據驅動 pricing 取代靜態常數。需 DB schema 擴充。

### 11.5 Auto-Renew 重新定價 (G14)

**問題**：Credit 到期 renew 時沿用原 rate，沒有根據當前市場重新定價。

**方案**：Renew 時走完整 pricing pipeline。

### 11.6 智慧貼牆策略 (G15)

**問題**：靠近 wall 只盲目降 1%。Wall 提供的是定價資訊而非威脅。

**方案**：`targetRate = wallRate - minTickSize`，搶先排在 wall 前面被吃到。

### 11.7 Order Book Gap Detection (G16)

**問題**：Order book 的 rate 空隙代表無競爭者的機會，目前未利用。

**方案**：掃描 book 找最近的 gap，在空隙中報價以最大化 fill probability。

---

## 12. 2026-03-21 策略設計層面 Review — 根本性收益盲點

> 以虛擬貨幣放貸專家角度審視策略設計本身（非實作差異）。完整分析見 `docs/strategy-journal.md`。

### 12.1 定價改為相對 bestAsk 偏移 (S1)

**問題**：§2.6 的定價基於 `FRR × MDC_premium`，但 Bitfinex offer 按利率排序 + FIFO 匹配。真正的競爭力取決於相對 bestAsk 的位置，不是相對 FRR 的乘數。比 bestAsk 低 1 tick 幾乎肯定成交，高 2% 可能永遠排不到。

**方案**：
```
targetRate = bestAsk - tickOffset(MDC, regime)
```
- MDC 強烈看漲 → offset ≈ 0（甚至 +1 tick，報比 bestAsk 高）
- MDC 中性 → offset = 1-2 ticks（搶先排隊）
- MDC 看跌 → offset = 3-5 ticks（積極求成交）

### 12.2 Auto-Renew 預設開啟 (S1)

**問題**：§7.2 說「永遠關閉 auto-renew」。但 auto-renew 是免費保險——引擎宕機時資金空轉 APY=0%。

**方案**：反轉邏輯：
- Auto-renew **永遠開啟**作為安全網
- 引擎正常時，在到期前**主動取消 auto-renew**並走完整 pricing pipeline 重新定價
- Auto-renew 只在引擎失能時實際觸發，防止資金閒置

### 12.3 移除隨機噪音、保留心理價位避讓 (S1)

**問題**：§4.14 的隨機擾動來自交易機器人（防前跑），但放貸不是零和博弈，沒有前跑風險。隨機 noise 只是在燒錢。

**方案**：
- 保留 §4.7 心理價位避讓（有效減少整數價位競爭）
- 移除或限制 §4.14 的隨機 noise。如果保留，noise 應只有 **+ 方向**（永不低於目標 rate）

### 12.4 新增 RatePercentile 信號 (S2)

**問題**：MDC 框架用 6 個信號預測「利率會漲還是跌」（交易思維），但放貸利率是**均值回歸**的。真正重要的問題是「當前利率在歷史分佈中排第幾」。

**方案**：
```
RatePercentile = percentile_rank(current_rate, rate_history_7d)
```
- `> P75`：積極放貸 + 鎖長期（利率高於歷史 75%）
- `P25–P75`：正常策略
- `< P25`：短期或等待（利率偏低，均值回歸大概率會上升）

此信號可作為 MDC 的**第 7 個信號源**或作為**獨立的 pre-filter**（低百分位時直接降低部署比例）。

### 12.5 清算瀑布分階段回應 (S2)

**問題**：§3.5 的瀑布回應是 `MDC = +1.0` 全量覆寫 + 統一 7-14d，但瀑布有三個階段，每階段最佳策略不同。

**方案**：

| 階段 | 時間 | 特徵 | Period | Rate |
|------|------|------|--------|------|
| 早期 | 0-30min | 利率飆升 | **2d**（捕捉瞬間暴利，快速回收再部署） | 最高可能 |
| 中期 | 30min-2hr | 高位盤整 | **7-14d**（spike 已確認，鎖住高利率） | 高於 FRR |
| 後期 | 2hr+ | 利率回落 | **停止新增** | N/A |

判定：追蹤 `cascadeStartTime` + 利率趨勢。利率開始下降 → 進入後期。

### 12.6 Weekend Premium 動態化 (S2)

**問題**：§5.4 設計固定 +2~5% 溢價，但實際週末利率比平日高 20-50%（正常市場）甚至 3-5×（大行情），固定值嚴重低估。

**方案**：
```
weekendPremium = rolling_4wk_weekend_avg_rate / rolling_4wk_weekday_avg_rate
```
- 正常市場可能得到 1.3（+30%）
- 死水市場可能得到 1.05（+5%）
- 大行情可能得到 2.0+（+100%）

自適應追蹤，不再用固定值猜測。

### 12.7 Per-Currency 參數組 (S3)

**問題**：所有幣種共用同一套 MDC 權重、regime 閾值和 premium 映射。但 fUSD（低波動、高流動性）和 fETH（高波動、事件驅動）的最佳策略完全不同。

**方案**：§2.2 權重和 §4.1 閾值改為 per-currency 或至少分兩組：

| 參數 | Stablecoin (fUSD/fUST) | Crypto (fBTC/fETH) |
|------|------------------------|---------------------|
| Book 消耗權重 | 降至 15%（流動性深，信號弱） | 提至 30%（流動性淺，信號強） |
| Regime 進入閾值 | ±0.4（穩定，需更大偏離才確認） | ±0.25（波動大，要快速反應） |
| Period 偏好 | 偏長（利率穩定，鎖長期） | 偏短（利率多變，保持靈活） |

### 12.8 信心度 × 部署比例 (S3)

**問題**：MDC 只影響 rate 和 period，不影響部署多少資金。信號矛盾時不應全量部署。

**方案**：
```
avgConfidence = mean(signal_i.Confidence for active signals)
deploymentRatio = 0.5 + 0.5 × |MDC| × avgConfidence
actualAmount = available × deploymentRatio
```
- 信號一致看漲（MDC=0.8, avgConf=0.9）→ 部署 86%
- 信號矛盾（MDC=0.1, avgConf=0.5）→ 部署 52.5%，保留彈藥等待更明確信號

### 12.9 滾動放貸與到期梯隊管理 (S3)

**問題**：§5.7 的到期分散是防禦性的（避免集中衝擊），缺少主動的到期結構管理。

**方案**：維持三梯隊到期結構：
- **1/3 短期**（2-3d）：高流動性，捕捉 spike
- **1/3 中期**（7-14d）：平衡收益和靈活性
- **1/3 長期**（21-30d）：鎖定穩定收益

到期時根據 RatePercentile（12.4）決定續約天數：
- `> P75` → 轉長期（鎖住高利率）
- `P25–P75` → 維持同梯隊
- `< P25` → 轉短期（等待回升）

### 12.10 P(higher_rate) 均值回歸公式 (S3)

**問題**：§4.8 的 EV_wait 需要 `P(higher_rate)`，spec 說「歷史統計」但無具體公式。

**方案**：利用利率均值回歸特性：
```
β = mean_reversion_coefficient (from historical regression)
σ = rate_volatility (rolling 7d)
μ = EMA_7d(rate)

P(higher_rate | wait t hours) = Φ((μ - current_rate) / (σ × √t))
```
- 利率遠低於均值 → P 高 → 等待
- 利率遠高於均值 → P 低 → 立即放貸 + 鎖長期

---

## 13. 2026-03-21 市場微觀結構 Review — 隱形收益殺手

> 從 Bitfinex 放貸市場的微觀結構和手續費結構角度審視。完整分析見 `docs/strategy-journal.md`。

### 13.1 Bitfinex 15% 手續費納入計算 (M1)

**問題**：整份 spec 未考慮 Bitfinex 對 funding earnings 收取的 **15% 手續費**。有效日利率只有名義的 85%。所有涉及 rate 比較的邏輯（floor、period 選擇、EV 模型）都有系統性偏差。

**影響**：

1. **Period 決策**：短期（2d）vs 長期（30d）的複利效率比較在加入手續費後結論改變。穩定利率下，長期單省去「成交空檔」和手續費重複計算，略優於短期多次複利。
2. **Floor 計算**：§4.9 的 floor 應基於淨利率 `rate × 0.85`，而非毛利率。
3. **EV 模型**：§4.8 的 `EV_deploy` 應為 `current_rate × 0.85 × expected_duration`。

**方案**：全局引入 `FEE_RATE = 0.15` 常數，所有 rate 比較和 EV 計算使用 `netRate = rate × (1 - FEE_RATE)`。

### 13.2 成交空檔追蹤與預排程 (M1)

**問題**：Credit 到期到新 offer 成交之間的「空檔」是隱形的 capital downtime。假設每次平均 20 分鐘：

| Period | 月到期次數 | 空檔總時間 | Capital Downtime |
|--------|-----------|-----------|-----------------|
| 2d | 15 | 5hr | 0.69% |
| 7d | 4.3 | 1.4hr | 0.19% |
| 14d | 2.1 | 0.7hr | 0.10% |
| 30d | 1 | 0.3hr | 0.05% |

**方案**：
1. 追蹤 `avgGapMinutes`（每次到期到成交的實際耗時）
2. Period 決策加入 gap cost：`gapCost(period) = avgGapMinutes / (period × 1440)`
3. **到期前預排程**：到期前 1 個心跳提前掛好下一筆 offer（如果有閒置餘額），到期瞬間新 offer 已在 queue

### 13.3 Proactive Offer Refresh (M1)

**問題**：Bitfinex 同利率 FIFO 匹配。排太深的 offer 可能等很久，但 §4.12 只在 stale 時才撤。

**方案**：主動搶隊首——如果 offer 超過 `refreshAge` 且 queueRatio > 0.3：
```
cancel(offer)
newRate = bestAsk - 1tick
place(newOffer at newRate)
```

與 §7.1 隊首保留互補：
- 已在隊首 → **保留**（§7.1 邏輯）
- 排太後面 → **主動 refresh**（本節邏輯）

### 13.4 提前歸還風險溢價 (M2)

**問題**：§5.6 的提前歸還修正只降低天數偏好權重，但沒有量化**逆向選擇**風險：
- 利率下跌 → 借方還舊借新 → 你被提前歸還（壞情況）
- 利率上漲 → 借方不還 → 你被鎖住（也是壞情況）

這是**負凸性**（callable bond risk）。長期單需要額外補償。

**方案**：
```
earlyReturnPremium = historicalEarlyReturnRate × (period / 30) × 0.05
adjustedRate = targetRate + earlyReturnPremium
```
- 天數越長 → 溢價越大
- 歷史提前歸還率越高 → 溢價越大
- 2 天單溢價 ≈ 0（幾乎不會被提前歸還）

### 13.5 非清算性 Rate Spike 偵測 (M2)

**問題**：§3.5 只偵測清算瀑布，但大戶開倉、套利需求、流動性事件也會導致利率飆升，且不觸發清算瀑布信號。

**方案**：獨立的 `RateSpike` 偵測器：
```
if currentRate > EMA_1h × spikeThreshold:  // spikeThreshold = 2.0
    spike = true
    action: 立即放貸，短期（2d），搶住高利率
```

與清算瀑布的區別：
- 清算瀑布：有大額清算流 + 硬性 MDC 覆寫 + 7-14d period
- Rate Spike：純利率異常 + 不覆寫 MDC + 短期 2d（因為不確定持續性）

### 13.6 FRR 操縱防護 (M2)

**問題**：FRR 是所有 active funding 的加權平均。大戶可用 wash lending（低利率自借自貸）拉低 FRR，你的 floor（FRR × 0.90）跟著被拉低。

**方案**：
```
bookMidRate = (bestBid + bestAsk) / 2  // 不受 active credits 影響
effectiveFRR = max(FRR, bookMidRate × 0.9)
```

偵測操縱：如果 `FRR < bookMidRate × 0.7`（FRR 明顯低於 book），標記為可疑，降低 FRR 在定價中的權重。

### 13.7 Temporal Laddering (M3)

**問題**：§4.2 的 allocation 同時掛出所有 offer，全部基於同一個 snapshot 的 bestAsk。但利率每分鐘都在變。

**方案**：跨心跳分批部署：
- T+0：部署 1/3 at current bestAsk（立即成交）
- T+3min：部署 1/3 at current bestAsk（可能已變化）
- T+6min：部署 1/3 at current bestAsk

類似 DCA，分散時間風險。在波動市場中，至少有一批可能捕捉到更好利率。

### 13.8 FRR 反饋迴路意識 (M3)

**問題**：管理的資金佔市場 total funding 比例上升時，你的 credits → 影響 FRR → 影響你的定價 → 影響你的 credits，形成正反饋迴路。

**方案**：
```
marketShare = managedFunding / totalMarketFunding
if marketShare > 0.05:
    frrWeight *= (1 - (marketShare - 0.05) × 2)  // 線性降低 FRR 權重
    // marketShare 10% → frrWeight 降 10%
    // marketShare 20% → frrWeight 降 30%
```

超過 5% 市佔時，逐步降低 FRR 對定價的影響，改用 order book 原始數據。

---

## 變更日誌（完整版本歷史）

### V13 市場微觀結構 Review (2026-03-21)

| 模組 | 變更內容 |
| :--- | :--- |
| 13.1 手續費 | 新增 Bitfinex 15% 手續費對 floor/period/EV 的全局影響 |
| 13.2 成交空檔 | 新增 gap cost 量化 + 到期前預排程機制 |
| 13.3 Offer Refresh | 新增主動搶隊首邏輯（排隊 >30% 深度時撤單重掛） |
| 13.4 提前歸還溢價 | 新增 callable risk 溢價公式（負凸性補償） |
| 13.5 Rate Spike | 新增非清算性利率飆升偵測器（獨立於 §3.5） |
| 13.6 FRR 防操縱 | 新增 effectiveFRR = max(FRR, bookMidRate×0.9) |
| 13.7 Temporal Ladder | 新增跨心跳分批部署（時間分散 vs 利率分散） |
| 13.8 FRR 反饋迴路 | 新增市佔率感知的 FRR 權重調整 |

### V12 策略設計層面 Review (2026-03-21)

| 模組 | 變更內容 |
| :--- | :--- |
| 12.1 bestAsk 定價 | 定價邏輯從 FRR 乘數改為 bestAsk 偏移，反映 Bitfinex FIFO 匹配機制 |
| 12.2 Auto-Renew 反轉 | 預設開啟作為安全網，引擎主動管理到期，取代 §7.2 的「永遠關閉」 |
| 12.3 Noise 簡化 | 移除隨機擾動、保留心理價位避讓，noise 限制為 + 方向 |
| 12.4 RatePercentile | 新增第 7 信號源：當前利率在 7d 歷史分佈的百分位，直接指導放/等決策 |
| 12.5 瀑布分階段 | 清算瀑布從單一回應改為三階段（早期 2d/中期 7-14d/後期停止） |
| 12.6 Weekend 動態化 | 固定 +2-5% 改為滾動 4 週 weekend/weekday ratio |
| 12.7 Per-Currency | MDC 權重和 regime 閾值分 stablecoin / crypto 兩組 |
| 12.8 信心度部署 | deploymentRatio = 0.5 + 0.5 × \|MDC\| × avgConfidence |
| 12.9 滾動放貸 | 新增到期梯隊管理（1/3 短 + 1/3 中 + 1/3 長），到期時按 RatePercentile 決定續約天數 |
| 12.10 均值回歸 | P(higher_rate) 改用 Ornstein-Uhlenbeck 公式，取代模糊的「歷史統計」 |

### V11 實作同步 + 策略 Review (2026-03-21)

| 模組 | 變更內容 |
| :--- | :--- |
| 全文件 | 新增 `⚡ 實作狀態` 標注，同步規範與實際程式碼的差異 |
| 2.2 信號權重表 | 新增「實作 λ」欄位，標記與設計值的偏差 |
| 2.5 MDC 計算 | 標注 Confidence 因子的額外實作 |
| 2.6 MDC 策略映射 | 標注實作改用線性內插（max ×1.50），非離散區間（max ×1.03） |
| 3.1 Dust Filter | 標注實作改用中位數百分比（5%），非固定 USD 門檻 |
| 3.2 巨單牆偵測 | 標注實作改用百分比門檻（5%），非 50,000 USD 絕對下限 |
| 3.7 競爭者偵測 | 標注實作改用統計偵測（followRate + roundRatio） |
| 4.1 體制識別 | 標注命名（Contango/Backwardation）、閾值（±0.3）差異 |
| 4.2 資金分層 | 標注實作改用餘額門檻分層，非雙窗口波動度 |
| 4.9 利率地板 | 標注實作改用三層 floor（FRR × 0.80），非 P10 百分位 |
| 4.10 閃崩保護 | 標注偵測閾值（-30%）和 cooldown（5min）差異 |
| 4.12 排隊估算 | 標注實作改用深度比例，非 ETA |
| 4.14 隨機擾動 | 標注實作改用百分比（±1%），非絕對值 |
| 5.4 週末溢價 | 標注實作為 rate 乘數，非天數遞減 |
| 5.5 事件日曆 | 標注實作為月末/季度固定溢價，未整合外部事件源 |
| 11. 策略 Review | 新增 §11.1-11.7：Pipeline 接線、閒置急迫度、FRR 趨勢、Fill Rate 學習、Renew 重定價、智慧貼牆、Gap Detection |

### V10 SaaS Multi-Tenant 擴展

| 模組 | 變更內容 |
| :--- | :--- |
| 1. 系統核心目標 | 新增多租戶 SaaS 平台目標 |
| 6.2 API 預算管理 | 新增共享層/Worker 歸屬標記，配額由全局分配器動態決定 |
| 9.1 成交紀錄 | 新增 `user_id` 欄位，所有紀錄綁定租戶 |
| 9.3 自適應回饋 | 改為 per-user 獨立執行 |
| 10.1 架構概述 | 新增共享層 + 私有層的 SaaS 系統架構圖 |
| 10.2 共享市場數據服務 | 新增 MarketSnapshot 結構與廣播機制 |
| 10.3 Per-User Worker | 新增 Worker 生命週期管理、隔離保證 |
| 10.4 API Key 安全 | 新增 AES-256-GCM 加密、權限驗證、審計日誌 |
| 10.5 全局配額分配器 | 新增三池配額模型（共享層 / 操作池 / Per-User 池） |
| 10.6 用戶策略參數 | 新增三層自訂（基礎 / 進階 / 專家）+ JSONB 儲存 |
| 10.7 計費系統 | 新增混合計費模型（基礎月費 + Alpha 分潤） |
| 10.8 前端儀表板 | 新增核心頁面定義與即時推送架構 |
| 10.9 監控與告警 | 新增系統層級 + 用戶層級雙層監控 |
| 10.10 資料庫 Schema | 新增完整 PostgreSQL schema（users, api_keys, executions, billing 等） |
| 10.11 部署架構 | 新增三階段擴展計劃（單機 → 拆分 → K8s） |
| 附錄 A 流水線 | 新增 SaaS 分割線標記（共享層 Phase 0-3 / Worker Phase 4-7） |
| 附錄 B.8 | 新增 SaaS 平台參數表（配額比例、Worker 上限、加密算法等） |

### V9 → V10 (Final Edition)

| 模組 | 變更內容 |
| :--- | :--- |
| 2.4 信號復甦平滑 | 新增信號恢復時的 3 步線性遞增機制，防止 MDC 因過時信號突然恢復而劇烈跳動 |
| 3.8 隱藏單深度估算 | 新增 Hidden_Ratio 估算，修正「可見競爭少」的假象，改善公開/隱藏單選擇 |
| 4.1 非對稱體制切換 | 向上跳躍確認期從 3 心跳縮短至 1 心跳，避免錯過暴漲初期 |
| 4.5 月內位置修正 | 日內時段效應加入月末/月初的溢價乘數 |
| 5.3 鎖倉機會成本折扣 | 新增基於波動度的天數折扣，高波動時自然傾向短天數快速周轉 |
| 5.7 到期時間分散 | 新增 ±1 天隨機偏移，避免自身資金集中到期壓低市場利率 |

### V8 → V9

| 模組 | 變更內容 |
| :--- | :--- |
| 2.3 信號時效性衰減 | 新增指數衰減因子 |
| 3.1 動態 Dust Filter | 門檻根據市場體制動態調整（200–500 USD） |
| 4.6 深度感知拆分 | 按各 tick 邊際排隊效率分配資金 |
| 4.8 機會成本框架 | 統一 EV 模型量化「借出 vs 待機」 |
| 4.11 自身市場衝擊 | 三級 Impact_Ratio 應對 |
| 6.4 冷啟動協議 | 三階段逐步啟用 |
| 7.1 利息即時再投資 | 每心跳掃描利息餘額即時投入 |

### V7 → V8

| 模組 | 變更內容 |
| :--- | :--- |
| 2. 複合信號框架 | MDC 加權評分系統 |
| 4.1 市場體制識別 | 四象限分類器 |
| 4.9 閃崩保護 | 利率閃崩偵測與暫停機制 |
| 6.1 事件驅動心跳 | WebSocket 事件觸發 |
| 6.2 API 預算管理 | 三級優先級隊列 |
| 6.3 優雅降級 | 數據源健康度監控與降級策略 |
| 8.1 動態偏離保護 | 根據體制浮動（20%–60%） |

### V6 → V7

| 模組 | 變更內容 |
| :--- | :--- |
| 2.4 Book 消耗速度 | 三級信號觸發 |
| 2.5 清算瀑布 | 清算資料流監控 |
| 2.6 跨幣種相關性 | BTC/ETH funding rate 領先指標 |
| 3.8 排隊位置估算 | ETA 估算與主動降價 |
| 3.9 部分成交管理 | 殘單偵測與重掛 |
| 4.1 曲線形態分析 | 駝峰/倒掛/平坦/陡峭識別 |
| 5.2 批次到期 | 5 分鐘匯集窗口 |

### V5 → V6

| 模組 | 變更內容 |
| :--- | :--- |
| 供需面信號 | 保證金持倉量追蹤 |
| 競爭者偵測 | 假退再進反制 |
| 雙窗口波動度 | 4h + 24h 雙窗口 |
| 掛單拆分 | 大額掛單自動拆分 |
| 日內時段效應 | 小時級溢價曲線 |
| 利率地板 | 動態 P10 下限 |
| 提前歸還率 | 有效回報修正天數 |
| 債權管理 | Auto-renew 策略 + 到期預排程 |

### V4 → V5

| 模組 | 變更內容 |
| :--- | :--- |
| 分散牆偵測 | 滑動窗口累計 5 檔 |
| 釣魚層回收 | 動態回收機制 |
| 雙速 VWAP | 慢速 VWAP 交叉確認 |
| 動態關卡粒度 | 根據市場狀態調整 |
| 事件日曆 | 合約交割/代幣解鎖/經濟數據 |
| 動態 TTL | 活躍 10-15min / 死水 45-60min |
| 原子化換單 | 先掛新單再撤舊單 |
| 績效追蹤 | Alpha 量化與策略歸因 |

---

## 附錄 A：心跳執行流水線 (Heartbeat Execution Pipeline)

### A.1 概述

每次心跳（定時或事件觸發）按以下**嚴格順序**執行。每個階段完成後才進入下一階段，確保數據依賴關係正確。整個流水線需在**單次心跳週期內**完成；若 API 預算不足，按 6.2 優先級犧牲低優先級數據。

> **多租戶 SaaS 模式下的流水線分割**（§10）：
> - **共享層執行**：Phase 0 → Phase 1（公開數據）→ Phase 1.5 → Phase 2 → Phase 3，由共享市場數據服務執行一次，結果打包為 `MarketSnapshot` 廣播。
> - **Per-User Worker 執行**：接收 `MarketSnapshot` → Phase 1（私有數據：錢包、掛單）→ Phase 4 → Phase 5 → Phase 6 → Phase 7。
> - 下方流水線以完整的單用戶視角描述，SaaS 模式下的分割邊界以 `── SaaS 分割線 ──` 標記。

### A.2 執行階段

```
Phase 0: 前置檢查
│
├─ 冷啟動判定（6.4）：若處於冷啟動階段，跳至簡化流程（純 FRR 跟隨）
├─ 閃崩保護檢查（4.10）：若閃崩凍結中，僅執行 Phase 1 數據拉取 + Phase 7 紀錄，跳過掛單操作
└─ API 預算分配（6.2）：計算本次心跳可用配額，預留 40% 給操作類

Phase 1: 數據拉取（依 API 優先級）
│
├─ [P0 必拉] Order Book、錢包餘額、活躍掛單清單
├─ [P1 WebSocket] 成交流快取、清算流快取（已持續監聽，讀取最新值）
├─ [P2 條件拉取] 持倉量、跨幣種 Funding Rate（依頻率排程判斷是否本次拉取）
└─ 數據源健康度更新（6.3）：標記各數據源為健康/警告/故障

Phase 1.5: 閃崩偵測（即時保護）
│
└─ 閃崩偵測（4.10）：基於 Phase 1 拉取的原始市場利率（Order Book 最優利率）判定。
   若 1 分鐘內下跌 >50%，標記凍結狀態，本次跳過 Phase 5-6（僅繼續執行 Phase 2-3-4 以更新內部狀態 + Phase 7 紀錄）。
   注意：此處讀取的是原始市場利率，不依賴 Phase 4 計算的目標利率。

Phase 2: 信號計算
│
├─ 各信號源獨立計算原始信號值（-1 ~ +1）
│   ├─ Book 消耗速度（3.4）
│   ├─ 清算瀑布判定（3.5）
│   ├─ 持倉量變化率（3.3）
│   ├─ 雙速 VWAP（4.4）
│   ├─ 跨幣種 Funding Rate（3.6）
│   └─ 日內時段效應 + 月內修正（4.5）
├─ 信號時效性衰減（2.3）：計算各信號的 Effective_Weight
├─ 信號復甦平滑（2.4）：恢復中的信號套用線性遞增
├─ 降級權重調整（6.3）：故障信號移除，權重重分配
└─ 隱藏單深度估算更新（3.8）

Phase 3: MDC 計算與體制判定
│
├─ MDC = tanh(Σ Signal_i × Effective_Weight_i)
├─ 清算瀑布硬性覆寫檢查（2.7）：若觸發，MDC = +1.0
├─ 市場體制判定（4.1）：含非對稱切換確認邏輯
├─ 載入體制參數組（Dust Filter 門檻、偏離閾值、分層比例等）

─────────── SaaS 分割線 ───────────
共享層輸出 MarketSnapshot，廣播給所有 Per-User Worker。
以下由各 Worker 獨立執行（帶用戶專屬參數）。
Worker 先拉取私有數據：錢包餘額、活躍掛單（計入 Worker 配額）。
───────────────────────────────────

Phase 4: 利率與天數決策（Per-User）
│
├─ 利率曲線形態分析（5.1）：判定陡峭/駝峰/倒掛/平坦
├─ 基礎天數計算：線性縮放（5.2） → 鎖倉折扣（5.3）
├─ 天數修正：週末溢價（5.4）、事件日曆（5.5）、提前歸還率（5.6）
├─ 到期時間分散（5.7）：±1 天隨機偏移
├─ 基礎利率計算：MDC 策略映射（2.6） → 溢價係數
├─ 利率修正：巨單牆避讓（3.2）、心理價位避讓（4.7）
├─ 機會成本評估（4.8）：EV_deploy vs EV_wait → 借出或待機
├─ 利率地板檢查（4.9）：硬性 P10 下限
├─ 市場偏離保護（8.1）：動態閾值校準
└─ 隨機擾動（4.14）：最終利率加入微量噪聲

Phase 5: 掛單執行
│
├─ 自身市場衝擊評估（4.11）：計算 Impact_Ratio，決定操作模式
├─ 資金分層佈署（4.2）：根據體制參數分配三層
├─ 深度感知拆分（4.6）：大額掛單按 tick 深度分配
├─ 隱藏單判斷（4.3）：結合 Hidden_Ratio 決定公開/隱藏
├─ 利息再投資掃描（7.1）：可用餘額 > 52 USD 納入資金池
├─ 部分成交管理（4.13）：殘單偏離檢查
├─ 排隊位置估算（4.12）：ETA 判斷是否提前降價
├─ 僵屍單 TTL 檢查（7.1）：超時掛單撤銷
├─ 隊首保留檢查（7.1）：誤差 < 0.00000002 且天數同 → 保留
├─ 批次到期檢查（7.2）：是否有即將到期的債權需預排程
├─ 原子化換單執行（7.1）：先掛新單再撤舊單（或回退傳統流程）
└─ 競爭者偵測（3.7）：掛單後監聽是否觸發假退再進

Phase 6: Auto-Renew 狀態管理
│
└─ 防禦性 auto-renew 檢查（7.2）：根據 API 健康度決定開/關

Phase 7: 紀錄與回饋
│
├─ 成交紀錄寫入（9.1）：含 MDC、體制、衝擊比率、鮮度等全欄位
├─ 策略標籤標記（9.2）：記錄觸發了哪些策略模組
└─ 計時器重置：設定下一次定時心跳時間
```

### A.3 衝突解決規則

當多個機制在同一心跳內產生矛盾指令時，按以下優先級裁決（高優先級覆寫低優先級）：

| 優先級 | 機制 | 說明 |
| :--- | :--- | :--- |
| **1（最高）** | 閃崩保護（4.10） | 凍結期間一切掛單操作暫停，無例外 |
| **2** | 清算瀑布硬性覆寫（2.7） | 強制 MDC = +1.0，覆寫所有信號 |
| **3** | 利率地板 P10（4.9） | 硬性下限，即使 EV 模型判定借出也不突破 |
| **4** | 市場偏離保護（8.1） | 偏離度硬性上限 80% |
| **5** | 機會成本框架（4.8） | 動態 EV 決策 |
| **6** | MDC 策略映射（2.6） | 標準決策邏輯 |
| **7（最低）** | 各微調策略 | 心理價位、隨機擾動等 |

### A.4 事件心跳的簡化流程

事件心跳（6.1）觸發時，為確保即時性，可跳過以下步驟：

- **可跳過**：P2 數據拉取（使用快取）、批次到期檢查（非即時需求）。
- **不可跳過**：P0 數據拉取、MDC 計算、掛單執行。

---

## 附錄 B：系統參數總表 (Complete Parameter Reference)

> 所有可調參數的集中索引。「自適應」欄標記該參數是否納入 9.3 的每週自動微調。

### B.1 複合信號框架參數

| 參數名 | 所屬模組 | 初始值 | 合理範圍 | 自適應 | 說明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Book 消耗速度基礎權重 | 2.2 | 25% | 15%–35% | ✅ | MDC 中的信號權重 |
| 清算瀑布基礎權重 | 2.2 | 20% | 10%–30% | ✅ | |
| 持倉量基礎權重 | 2.2 | 20% | 10%–30% | ✅ | |
| VWAP 動量基礎權重 | 2.2 | 15% | 5%–25% | ✅ | |
| 跨幣種 Funding Rate 基礎權重 | 2.2 | 10% | 5%–20% | ✅ | |
| 日內時段基礎權重 | 2.2 | 10% | 5%–20% | ✅ | |
| λ_Book 消耗 | 2.3 | 0.05 | 0.02–0.10 | ✅ | 信號衰減速率（實作：**0.01**） |
| λ_清算瀑布 | 2.3 | 0.03 | 0.01–0.06 | ✅ | （實作：**0.005**） |
| λ_持倉量 | 2.3 | 0.005 | 0.002–0.01 | ✅ | （實作：**0.008**） |
| λ_VWAP | 2.3 | 0.01 | 0.005–0.02 | ✅ | （實作：0.01 ✅） |
| λ_Funding Rate | 2.3 | 0.002 | 0.001–0.005 | ✅ | （實作：**0.005**） |
| λ_日內時段 | 2.3 | 0.001 | 0.0005–0.003 | ✅ | （實作：**0.002**） |
| 復甦平滑步數 | 2.4 | 3 | 2–5 | ✅ | 信號恢復的遞增數據點數 |
| MDC 強烈看漲閾值 | 2.6 | +0.7 | +0.6–+0.8 | ❌ | 策略映射邊界 |
| MDC 溫和看漲閾值 | 2.6 | +0.3 | +0.2–+0.4 | ❌ | |
| MDC 溫和看跌閾值 | 2.6 | -0.3 | -0.4–-0.2 | ❌ | |
| MDC 強烈看跌閾值 | 2.6 | -0.7 | -0.8–-0.6 | ❌ | |

### B.2 市場感知參數

| 參數名 | 所屬模組 | 初始值 | 合理範圍 | 自適應 | 說明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Dust Filter 門檻（牛市） | 3.1 | 500 USD | 300–800 | ✅ | |
| Dust Filter 門檻（熊市） | 3.1 | 200 USD | 100–400 | ✅ | |
| Dust Filter 門檻（震盪） | 3.1 | 300 USD | 200–500 | ✅ | |
| Dust Filter 門檻（危機） | 3.1 | 500 USD | 300–800 | ✅ | |
| MIN_WALL_THRESHOLD | 3.2 | 50,000 USD | 30,000–100,000 | ❌ | 巨單牆絕對下限 |
| 巨單牆倍數 | 3.2 | 5× Median | 3×–8× | ❌ | |
| 分散牆滑動窗口檔數 | 3.2 | 5 | 3–8 | ❌ | |
| 持倉量變化率閾值 ΔLong/ΔShort | 3.3 | 5% | 2%–10% | ✅ | |
| Book 消耗加速 σ 閾值 | 3.4 | 2σ | 1.5σ–3σ | ✅ | |
| Book 消耗瘋搶 σ 閾值 | 3.4 | 3σ | 2.5σ–4σ | ✅ | |
| 清算瀑布金額閾值 | 3.5 | 5,000,000 USD | 2M–10M | ✅ | |
| 清算瀑布回歸等待時間 | 3.5 | 15 分鐘 | 10–30 分鐘 | ❌ | |
| 清算瀑布回歸遞減步數 | 3.5 | 3 步 | 2–5 步 | ❌ | 硬性覆寫解除後的信號遞減數據點數 |
| 競爭者偵測時間窗口 | 3.7 | 10 秒 | 5–20 秒 | ❌ | |
| 競爭者偵測掛單數閾值 | 3.7 | 3 筆 | 2–5 筆 | ❌ | |
| 假退再進等待時間 | 3.7 | 30 秒 | 15–60 秒 | ❌ | |
| Hidden_Ratio 判定閾值 | 3.8 | 30% | 20%–50% | ✅ | |
| Hidden_Ratio 統計窗口 | 3.8 | 30 分鐘 | 15–60 分鐘 | ❌ | |

### B.3 執行策略參數

| 參數名 | 所屬模組 | 初始值 | 合理範圍 | 自適應 | 說明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 體制切換確認心跳數（向下/同級） | 4.1 | 3 | 2–5 | ❌ | |
| 體制切換確認心跳數（向上跳躍） | 4.1 | 1 | 1–2 | ✅ | 非對稱切換 |
| 趨勢牛市 MDC 閾值 | 4.1 | +0.5 | +0.3–+0.7 | ✅ | |
| 趨勢熊市 MDC 閾值 | 4.1 | -0.5 | -0.7–-0.3 | ✅ | |
| 危機模式 Sigma 閾值 | 4.1 | 歷史 +4σ | +3σ–+5σ | ✅ | |
| Sigma「高」判定乘數 | 4.2 | 1.5× 均值 | 1.2×–2.0× | ❌ | Sigma > 均值 × 此乘數判定為「高」 |
| Sigma「低」判定乘數 | 4.2 | 0.7× 均值 | 0.5×–0.9× | ❌ | Sigma < 均值 × 此乘數判定為「低」 |
| Sigma 均值回望天數 | 4.2 | 30 天 | 14–60 天 | ❌ | |
| 活躍期分層（流動/市場/釣魚） | 4.2 | 40/40/20 | — | ❌ | 體制參數組 |
| 死水期分層 | 4.2 | 60/40/0 | — | ❌ | |
| 插針分層 | 4.2 | 30/40/30 | — | ❌ | |
| 收斂分層 | 4.2 | 50/40/10 | — | ❌ | |
| 釣魚層回收心跳數 N | 4.2 | 5 | 3–8 | ❌ | |
| 碎片清掃門檻 | 4.2 | 52 USD | 51–55 | ❌ | |
| MDC 釣魚層加碼偏移幅度 | 2.6/4.2 | +5%~+10% | +3%~+15% | ❌ | 強烈看漲時從其他層移至釣魚層的比例 |
| MDC 前線加碼偏移幅度 | 2.6/4.2 | +5%~+10% | +3%~+15% | ❌ | 看跌時從釣魚層移至第一層的比例 |
| 隱藏單金額門檻 | 4.3 | 5,000 USD | 3,000–10,000 | ❌ | |
| 快速 VWAP 窗口 | 4.4 | 5 分鐘 | 3–10 分鐘 | ❌ | |
| 慢速 VWAP 窗口 | 4.4 | 20 分鐘 | 15–30 分鐘 | ❌ | |
| MDC 強制追高閾值 | 4.4 | +0.9 | +0.8–+1.0 | ❌ | |
| 日內高需求溢價信號 | 4.5 | +0.3~+0.5 | +0.2~+0.6 | ✅ | |
| 日內低需求溢價信號 | 4.5 | -0.3~-0.5 | -0.6~-0.2 | ✅ | |
| 月末乘數 | 4.5 | 1.1 | 1.0–1.2 | ✅ | |
| 月初乘數 | 4.5 | 1.05 | 1.0–1.15 | ✅ | |
| 拆分啟動金額 | 4.6 | 20,000 USD | 10,000–50,000 | ❌ | |
| 拆分掃描 tick 數 | 4.6 | 5 | 3–8 | ❌ | |
| 拆分單筆下限 | 4.6 | 5,000 USD | 3,000–10,000 | ❌ | |
| 拆分上限筆數 | 4.6 | 5 | 3–8 | ❌ | |
| Efficiency 計算權重 | 4.6 | — | — | ✅ | Consume_Rate vs Depth 的相對權重 |
| 大關卡列表 | 4.7 | 0.05%, 0.1%, 0.15%, 0.2% | — | ❌ | |
| 小關卡列表 | 4.7 | 0.02%, 0.03% | — | ❌ | |
| P(higher_rate) 基礎估算 | 4.8 | 依歷史統計 | — | ✅ | 機會成本模型 |
| opportunity_cost 回望天數 | 4.8 | 7 天 | 3–14 天 | ❌ | |
| Rate Floor P10 回望天數 | 4.9 | 30 天 | 14–60 天 | ❌ | |
| Rate Floor 安全閥待機時間 | 4.9 | 2 小時 | 1–4 小時 | ❌ | |
| Rate Floor 安全閥下調幅度 | 4.9 | 20% | 10%–30% | ❌ | |
| 閃崩偵測跌幅閾值 | 4.10 | 50% | 30%–70% | ❌ | |
| 閃崩偵測時間窗口 | 4.10 | 1 分鐘 | 30 秒–3 分鐘 | ❌ | |
| 閃崩凍結時間 | 4.10 | 60 秒 | 30–120 秒 | ❌ | |
| 閃崩恢復利率閾值 | 4.10 | 70% | 50%–80% | ❌ | |
| Impact_Ratio 中影響閾值 | 4.11 | 10% | 5%–15% | ✅ | |
| Impact_Ratio 高影響閾值 | 4.11 | 25% | 15%–35% | ✅ | |
| 分批操作間隔 | 4.11 | 5 秒 | 3–10 秒 | ❌ | |
| ETA 提前降價倍數 | 4.12 | 2× TTL | 1.5×–3× | ✅ | |
| ETA 保留閾值 | 4.12 | 0.5× TTL | 0.3×–0.8× | ✅ | |
| 主動降價 Ticks | 4.12 | 1–2 | 1–3 | ❌ | |
| 部分成交偏離閾值 | 4.13 | 5 Ticks | 3–8 Ticks | ❌ | |
| 隨機擾動範圍 | 4.14 | 1e-8 ~ 5e-8 | — | ❌ | （實作：**rate ±1%, amount ±2%**） |

### B.4 天數決策參數

| 參數名 | 所屬模組 | 初始值 | 合理範圍 | 自適應 | 說明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Slope 陡峭閾值 | 5.1 | >0 且 Curvature ≈ 0 | — | ✅ | 曲線形態判定 |
| Curvature 駝峰閾值 | 5.1 | — | — | ✅ | |
| Slope 平坦微小閾值 | 5.1 | — | — | ✅ | 需回測確定初始值 |
| SMA_7d 乘數 | 5.2 | 1.2 | 1.1–1.5 | ❌ | 動態利差門檻 |
| 線性縮放最高天數 | 5.2 | 30 天 | — | ❌ | Bitfinex 上限 |
| lockup_discount（低波動） | 5.3 | 0.02 | 0.01–0.05 | ✅ | 公式：Rate × (1 - discount × max(0, days-2) / 28) |
| lockup_discount（中波動） | 5.3 | 0.05 | 0.03–0.08 | ✅ | |
| lockup_discount（高波動） | 5.3 | 0.10 | 0.05–0.15 | ✅ | 2 天期折扣=0，30 天期折扣最大 |
| 週末溢價啟動時間 | 5.4 | UTC 週四 12:00 | — | ✅ | 建議回測 |
| 週末溢價結束時間 | 5.4 | UTC 週五 18:00 | — | ✅ | |
| 週末門檻降低幅度 | 5.4 | 20% | 10%–30% | ❌ | |
| 事件日曆提前啟動時間 | 5.5 | 24–48 小時 | 12–72 小時 | ❌ | |
| 持有率降權閾值 | 5.6 | 60% | 50%–70% | ✅ | |
| 到期分散偏移天數 | 5.7 | ±1 天 | ±1–±2 天 | ✅ | |

### B.5 系統架構參數

| 參數名 | 所屬模組 | 初始值 | 合理範圍 | 自適應 | 說明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 活躍期定時心跳間隔 | 6.1 | 3 分鐘 | 2–5 分鐘 | ❌ | |
| 死水期定時心跳間隔 | 6.1 | 15 分鐘 | 10–30 分鐘 | ❌ | |
| 事件心跳防抖間隔 | 6.1 | 30 秒 | 15–60 秒 | ✅ | |
| 大額成交事件閾值 | 6.1 | 100,000 USD | 50,000–200,000 | ❌ | |
| API 操作預算保留比例 | 6.2 | 40% | 30%–50% | ❌ | |
| P2 正常拉取頻率 | 6.2 | 每 2–3 心跳 | 每 2–5 心跳 | ❌ | |
| P2 死水期拉取頻率 | 6.2 | 每 5 心跳 | 每 3–8 心跳 | ❌ | |
| 數據源警告閾值 | 6.3 | 2 心跳 | 1–3 心跳 | ❌ | |
| 數據源故障閾值 | 6.3 | 5 心跳 | 3–8 心跳 | ❌ | |
| 恢復穩定確認心跳數 | 6.3 | 2 心跳 | 1–3 心跳 | ❌ | |
| 冷啟動 Phase 1 持續時間 | 6.4 | 10 分鐘 | 5–15 分鐘 | ❌ | |
| 冷啟動 Phase 2 持續時間 | 6.4 | 10 分鐘 | 5–15 分鐘 | ❌ | |
| 冷啟動 Phase 3 持續時間 | 6.4 | 10 分鐘 | 5–15 分鐘 | ❌ | |

### B.6 維護與安全參數

| 參數名 | 所屬模組 | 初始值 | 合理範圍 | 自適應 | 說明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 隊首保留利率誤差 | 7.1 | 0.00000002 | — | ❌ | |
| 活躍期 TTL | 7.1 | 10–15 分鐘 | 5–20 分鐘 | ❌ | |
| 死水期 TTL | 7.1 | 45–60 分鐘 | 30–90 分鐘 | ❌ | |
| 利息再投資門檻 | 7.1 | 52 USD | 51–55 | ❌ | |
| 防禦性 auto-renew 觸發退避次數 | 7.2 | 3 次 | 2–5 次 | ❌ | |
| 批次到期匯集窗口 | 7.2 | 5 分鐘 | 3–10 分鐘 | ❌ | |
| 批次到期最少筆數 | 7.2 | 2 筆 | 2–3 筆 | ❌ | |
| 偏離保護閾值（牛市） | 8.1 | 40% | 30%–50% | ✅ | |
| 偏離保護閾值（熊市） | 8.1 | 20% | 10%–30% | ✅ | |
| 偏離保護閾值（震盪） | 8.1 | 25% | 15%–35% | ✅ | |
| 偏離保護閾值（危機） | 8.1 | 60% | 40%–70% | ✅ | |
| 偏離保護硬性上限 | 8.1 | 80% | — | ❌ | 絕對安全邊界 |
| 錢包待機餘額門檻 | 8.2 | 50 USD | — | ❌ | Bitfinex 最低限制 |
| 指數退避最大重試次數 | 8.3 | 5 次 | 3–7 次 | ❌ | |
| 自適應回饋調整上限 | 9.3 | ±10% | ±5%–±15% | ❌ | 防過擬合約束 |

### B.7 需回測確定的參數

以下參數的初始值需要使用歷史數據回測後才能設定，文件中僅提供了概念定義：

| 參數名 | 所屬模組 | 說明 |
| :--- | :--- | :--- |
| Slope/Curvature 各形態的精確閾值 | 5.1 | 需根據歷史利率曲線回測分佈 |
| 週末溢價最佳啟動時間 | 5.4 | 需回測實際週末利差擴大的時間點 |
| 各時段溢價信號的精確值 | 4.5 | 需統計歷史小時級利率分佈 |
| P(higher_rate) 的歷史基準 | 4.8 | 需根據不同 MDC 斜率下的歷史利率變化統計 |
| 各天數區間的歷史持有率 | 5.6 | 需統計歷史債權的實際持有天數 |
| Hidden_Ratio 的歷史分佈 | 3.8 | 需比較歷史可見深度與實際成交速度 |

### B.8 多租戶 SaaS 平台參數

| 參數名 | 所屬模組 | 初始值 | 合理範圍 | 說明 |
| :--- | :--- | :--- | :--- | :--- |
| 共享層配額預留比例 | 10.5 | 15% | 10%–25% | 全局 rate limit 中分配給共享層的比例 |
| 操作預留池比例 | 10.5 | 40% | 30%–50% | 全局 rate limit 中分配給掛撤單的比例 |
| Per-User 數據池比例 | 10.5 | 45% | 25%–60% | 全局 rate limit 中分配給 Worker 私有數據的比例 |
| Worker 崩潰自動重啟上限 | 10.3 | 3 次/小時 | 2–5 | 超過則暫停並通知用戶 |
| API Key 權限覆核週期 | 10.4 | 24 小時 | 12–48 小時 | |
| API Key 加密算法 | 10.4 | AES-256-GCM | — | 不可更改 |
| MarketSnapshot 廣播方式 | 10.2 | Go channel | channel / Redis Pub | 單機用 channel，多機用 Redis |
| 用戶參數即時生效延遲 | 10.6 | 下一個心跳 | — | 修改後最遲在下一次心跳載入 |
| 計費週期 | 10.7 | 月結 | 月結 / 週結 | |
| 計費利潤基準 | 10.7 | FRR 被動收入 | — | Alpha = 實際 - FRR |
| Dashboard WebSocket 推送頻率 | 10.8 | 每心跳 | — | 即時狀態更新 |
| 審計日誌保留天數 | 10.4 | 365 天 | 90–730 天 | |
| Worker 快照更新頻率 | 10.9 | 每心跳 | — | 寫入 worker_snapshots 表 |
| 單機最大 Worker 數 | 10.11 | 500 | 100–1000 | 取決於機器規格 |
