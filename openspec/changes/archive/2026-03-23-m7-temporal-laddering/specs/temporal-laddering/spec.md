## ADDED Requirements

### Requirement: 三批次資金部署
Worker SHALL 將每個部署週期分為三個 tranche（批次），每次 tick 只部署可用資金的 1/3。每批使用當下最新的 MarketSnapshot 重新計算策略。

#### Scenario: 正常三批部署
- **WHEN** 可用餘額 = 3000 USD，trancheIndex 從 0 開始
- **THEN** T+0 部署 ~1000，T+1 部署 ~1000，T+2 部署 ~1000（每批金額因利率變動可能略有差異）

#### Scenario: 每批使用最新利率
- **WHEN** T+0 的 bestAsk = 0.0002，T+1 的 bestAsk = 0.00025
- **THEN** T+0 的 offer 用 0.0002 計算，T+1 的 offer 用 0.00025 計算

### Requirement: trancheIndex 狀態追蹤
Worker SHALL 維護 `trancheIndex`（0/1/2），每次成功部署 offer 後遞增。到達 3 時 MUST reset 為 0。

#### Scenario: 週期完成 reset
- **WHEN** trancheIndex=2 且該 tick 成功部署 offers
- **THEN** trancheIndex MUST reset 為 0

#### Scenario: tick 無部署不遞增
- **WHEN** tick 因 flash_freeze 或 insufficient_balance 跳過（decision.Offers 為空）
- **THEN** trancheIndex MUST 保持不變

### Requirement: Available 限縮比例
Worker SHALL 使用 `trancheRatios = [1/3, 1/2, 1]` 限縮 `DecisionContext.Available`，讓策略只看到部分可用餘額。

#### Scenario: tranche 比例計算
- **WHEN** 實際 Available=3000, trancheIndex=0
- **THEN** 策略看到 Available=1000（3000 × 1/3）

#### Scenario: 第二批自然平衡
- **WHEN** T+0 部署了 1000，實際剩餘 2000，trancheIndex=1
- **THEN** 策略看到 Available=1000（2000 × 1/2）

### Requirement: 小額跳過 laddering
Worker SHALL 在可用餘額 < minBalance × 2（100 USD）時跳過 laddering，一次部署全部資金。

#### Scenario: 小額直接部署
- **WHEN** Available=80 USD
- **THEN** trancheIndex MUST 保持 0，策略看到完整的 Available=80

### Requirement: Config 變更 reset
Worker SHALL 在收到 ReloadConfig 時 reset trancheIndex 為 0。

#### Scenario: 使用者改策略 mid-cycle
- **WHEN** trancheIndex=1 且收到 ReloadConfig
- **THEN** trancheIndex MUST reset 為 0，下一次 tick 從第一批重新開始
