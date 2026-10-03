---
title: 研究基礎建設 C/D/E — fill 依 tenor 定價＋窗尾截斷、book-replay fill 證據嚴格 fail-closed、weekly 重驗同服務後置且永不 auto-promote
date: 2026-09-22
status: active
tags: [bfx-funding-bot, decision, backtest, fill-model, live-validation, automation]
---

# 研究基礎建設 C/D/E：tenor 定價、book-replay fill、weekly 重驗

## Context

2026-09-22 策略系統 review（研究報告 2026-09-22-strategy-system-review，原文不在本 repo）的兩個核心發現：所有部署策略硬編碼 2 天期而 FRR 溢價來自期限結構（F2）；linear fill 模型在 quote=close 時恆為 1，backtest 分不出任何執行或期限差異（F3，[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md) R1 早已點名這個循環）。Will 接受依 D → C → E 順序實作。prior state：[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) D3 已規定 empirical fill 只能是有 scope/hash 的 artifact、linear 只可由明示離線指令使用；本 ADR 在那個契約上擴充，不改它。

## Options Considered

**A. tenor 定價序列（D）**
- A1 維持單一序列（a30 close 乘 2 天）
- A2 用 p2/p30 內插 term curve 給 7/14 天
- **A3（選用）**：exact tenor 序列優先（2→p2、30→p30），其餘退回 a30 聚合並在結果標記

**B. 跨窗鎖定的計帳（D）**
- B1 決策當下全額計入（現狀；fresh-per-window WFO 下 14/30 天鎖跨窗重複計）
- B2 引擎改線性 accrual（改變所有既有 gate／derivation 數字）
- **B3（選用）**：opt-in `truncate_at_window_end`，只計窗內天數，預設關閉
- B4 全歷史連續跑、月末取樣 equity（at-decision 計帳下月度分布塊狀）

**C. fill 證據來源與缺模型行為（C）**
- C1 維持 linear
- C2 接 G13 candle path-crossing（quote≤close 仍恆為 1）
- **C3（選用）**：book-replay learner 產 `source="book"` artifact；引擎 `EMPIRICAL_SOURCES={candle, book}`；per-series 模型嚴格：某 tenor 的定價序列沒模型 → 該 arm 標 not scored，不退回 linear
- C4 缺模型時退回 linear 並註記

**D. weekly 重驗（E）**
- D1 另開 timer／service
- **D2（選用）**：同一個 `weekly-report`，營運三步之後接研究步驟，每步 timeout＋fail-soft；規則預登記；永不 auto-promote
- D3 只手動跑

## Decision

- **D1 = A3**：`run_backtest(market_series_by_agg=…)`＋`resolve_market_series_key`；結果帶 `pricing_series_used`。
- **D2 = B3**：`BacktestConfig.truncate_at_window_end`，只有 period-structure runner 打開。
- **D3 = C3**：`book_replay.py`＋`learn_book_fill_rate`；`fill_models_by_agg` 嚴格 per-series；unknown source 仍 scope_mismatch。
- **D4 = D2**：compose 鏈擴充、systemd 1800→5400s；diff 規則＝champion 漂移（最近 12 窗中位數 < 全歷史 p25）與 challenger 反超（配對 CI 下界連續 3 週 >0）。

## Rationale

- **D1**：A1 正是 F2 的高估來源（a30 rate 記給 2 天鎖）。不選 A2：p7/p14 沒有真實成交序列，內插是發明數據；a30 至少是交易所真的聚合過的值，且報告逐 arm 標出「priced by」讓近似可見。代價：AP 的 7/14 tier 仍是近似，A0 深度審計未過前不能當精確值。
- **D2**：B1 在 30 天鎖上會讓每個月窗都從 hour 0 重新交易，等於每月多算一次；B2 修得最乾淨但會動 deploy gate 與 derive_cells 的位元穩定數字，風險與本輪目的不成比例；B4 讓月度統計失去意義。B3 用一個開關把語意變成「窗內鎖、窗內計」，對 always 類 arm 幾乎無損，對 AP 的 14 天尾巴略保守，且預設路徑位元不變（測試 `test_default_config_is_byte_stable_for_existing_callers`）。
- **D3**：C1/C2 在 spread=0 都無鑑別力，正是要修的東西。不選 C4：退回 linear 會讓「沒證據」與「有證據」外觀相同，違反 08-23 D3 的精神；標 not scored 讓 AP 在 book 模式下暫時無法評分，這是誠實的結果而非缺陷。代價：book-replay 對事後插隊的新掛單偏樂觀，絕對水準要等 live fills 校準（C4 in plan）。
- **D4**：D1 多一個 unit 與排程要維護，D3 會回到 07 月前的手動狀態。同服務後置讓營運報告永遠先出、研究失敗只留 WARN；代價是整條鏈時限拉長到 90 分鐘，liquidations topup 的 rate limit 是最可能吃掉預算的一步。永不 auto-promote 是 `/strategy-research` 紅線 2 的延伸。

## Expected Outcome

- D6（VM 全歷史 + fUST p30 + always_frr）與 book 模式各一份報告；`mr_a30_legacy_vs_mr_a30` > 0、`always_30d_vs_always_2d` 有雙 bound CI。
- 每週一主機 reports 目錄下 `<date>-weekly-research-{book,oos}.md` 產出且營運報告不受研究步驟影響。
- 成功判準：第一次 VM 手動跑完整鏈無步驟超時；drift／overtake 旗標第一次出現時走 registry review 而非任何自動變更。

## Followup

- 下次 release 帶上 compose 鏈、systemd 5400s、C0 快照 300s；部署後 VM 手動跑一次 `docker compose -f docker-compose.bot.yml --profile ops run --rm weekly-report`，看 liquidations topup 是否吃掉預算。
- VM：`learn_book_fill_rate` 必須先於 `run_period_structure_backtest --fill-model book`。
- C4 校準：放貸恢復後累積 fills，比對 `report_execution_quality` 的 submit→fill latency 與模型 ttf。
- A0 深度審計與 D6 全歷史四臂重跑（指令在 review §9）。

## Revocation Triggers

- A0 顯示 period 30 的 ask 深度常態不足 → `always_30d` 的 fill 假設失效，D1 對 30 天的結論降級為「等 live」。
- C4 校準顯示 book-replay ttf 系統性低估 2 倍以上 → 模型重設計，book 模式報告作廢。

## Lessons

### Rules

- **R1（跨窗鎖定的計帳）**：`Rule:` 每窗重置狀態的 walk-forward 評估遇到持有期可能長於測試窗的策略時，要先決定跨窗部位怎麼計，預設「決策當下全額計入」會讓長鎖每窗重複計；用窗尾截斷或 accrual，且讓預設路徑位元不變。

### Observations

- **O1**：三個 fill 假設下（linear α=5、always-fill、book）同一組 arm 的排序可能不同：mr_a30 在 α=5 對 always_2d 夾 0、always-fill 下 +0.08~0.12；只有雙 bound 一起看才知道結論靠不靠 fill 假設。

## Amendment (2026-09-27): fixtures 首跑數字與 2026-09-23 correctness 修正

### Amendment Decision

- **D5（fail-closed 延伸到 backtest 輸入）**：只要缺指定市場序列的 mts 或 close，一律 `BacktestIncomplete("market_series_gap")`，不再退回觀察價，也不再保證成交；`period_structure.align_series` 先對齊共同 slot，並在報告寫出丟掉幾格（fUST 1、fUSD 24）。
- **D6（fill horizon 邊界）**：G13 與 book-replay 兩個 learner 只計算「完整落在 (決策時刻, 截止] 內」的 candle；無法判定的 horizon 不當樣本，不足的 bucket 回報 unavailable。artifact 版本升為 `g13-candle-v2`／`book-replay-v2`（VM 當時還沒生成過任何舊版，所以沒有東西要作廢）。
- **D7（optimizer 證據）**：signal、maker、taker 三個候選各自用自己價格的 fill evidence 評分；沒有自身 evidence 的候選不參與評分；`optimizer_live` 的送單 gate 帶選中候選的 evidence。

### Amendment Rationale

- 三項都是已重現的缺陷（缺價仍 100% 成交、60 分鐘 horizon 算進 83 分鐘成交量、共用 evidence 讓候選排名翻轉）。修法都選「標 unknown／不評分」而非「補一個近似值」，理由與 D3 不選 C4 相同：沒證據和有證據的結果不能看起來一樣。代價是更多窗口與候選變成 not scored。重跑 fixtures 後各數字變動 ≤0.0002%/mo，結論不變。

### Amendment Result

- `git log --oneline 85db546 -1`（D5/D6）、`f13d9c1`（D7）。fixtures 首跑（2022-01..2026-05，α=5 ／ α=1e-9）：fUSD `always_30d` 對 `always_2d` +0.47%/mo CI [0.34, 0.61]（兩個 bound 相同，年化 11.5% 對 5.5%）；`mr_a30_legacy − mr_a30` +0.077（fUST）／+0.131（fUSD），證實 a30 錯配把 backtest 灌水；`mr_a30` 對 `always_2d` 在 α=5 夾 0、always-fill 下 +0.08／+0.12，因此取決於 fill；AP 對 `always_2d` +0.105／+0.181，兩個 bound 的 CI 都排除 0。
- 重現：`cd backend && uv run python -m scripts.run_period_structure_backtest`（fixtures 或 DB 模式；`--fill-alpha 1e-9` 為上界；`--fill-model book` 要先跑 `scripts.learn_book_fill_rate`）。

## Related

- 來源：本 ADR 即原始紀錄（2026-09-22 與 coding agent 討論當場拍板）。實作 plan：`2026-09-22-research-infra-c-d-e.md`（原文已不在 repo，本 ADR 即紀錄）。
- Review 全文：研究報告 2026-09-22-strategy-system-review（原文不在本 repo）。§3 → AP ADR Amendment、§4 → [2026-09-22-frr-baseline-ab-via-sub-account](2026-09-22-frr-baseline-ab-via-sub-account.md)、§5–§7 → 本 ADR、§8 → [2026-06-06-signal-eda-funnel](2026-06-06-signal-eda-funnel.md) 2026-09-27 Amendment。
- Fixtures 報告：2026-09-22-period-structure-fixtures、2026-09-22-period-structure-fixtures-fill1e-9（各＋同名 `.json`，含 per-window 數據供 `diff_research_report`；原文不在本 repo，產出 commit `d80dc1f`、`85db546`）。
- Correctness plan：`2026-09-22-strategy-correctness-fixes.md`（原文已不在 repo，本 ADR 即紀錄）。
- 實作 commits：D `d80dc1f`、C0 `d6a337e`、C `ba9a600`、E `a93612b`；A0 審計 `a528eb0`。
- 契約前提：[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) D3（artifact 契約）；循環論證的原始指認：[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md) R1；期限是槓桿的原始 thesis：[2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md) D1。
- 同日裁決：[2026-09-22-frr-baseline-ab-via-sub-account](2026-09-22-frr-baseline-ab-via-sub-account.md)。
- 規則落點：strategy registry「Weekly re-validation rules（E3）」段（不在本 repo）。
