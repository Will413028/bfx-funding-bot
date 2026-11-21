---
title: 掛單價一律跟著 book，不再以整點訊號利率當下限
date: 2026-11-22
status: active
supersedes: "[2026-07-06-e2-book-aware-clamp](2026-07-06-e2-book-aware-clamp.md) 的 D2（down-clamp 以 15% 為 floor）"
tags: [bfx-funding-bot, decision, execution, pricing]
---

# 掛單價一律跟著 book，不再以整點訊號利率當下限

> owner 於 2026-11-22 核可（選項 A）。

## Context

新單的價格由 `PeriodPricer` 用即時 funding book 對 exact period 決定：有夠深的 bid 願意付訊號利率就掛訊號利率（taker），否則掛「最低賣價減一檔」。但 book 的價比整點訊號利率低超過 15% 時，它改掛訊號利率（`signal_floor` 分支，`BFX_BOOK_MAX_DOWN_PCT=0.15`）。這條下限來自 E2 clamp 的 D2，當時的理由是利率崩跌時不要用舊收盤價賤賣。

2026-11-21 16:51 UTC 恢復放貸後的前三張單都走了這個分支：fUST a30 cell 的整點訊號利率是 0.00029645／日，同一時間 2 天期 book 最低賣價是 0.0001334、最高買價 0.0001301。三張各 200 UST 的單掛在市場的 2.2 倍，沒有成交。訊號利率每小時才更新一次，撤單重掛（E1）要等單掛滿 30 分鐘，而且重掛時仍套同一條下限，所以資金在這段時間等同閒置。

**約束**：

- `external` 借方只會由低往高吃賣單；掛在最低賣價上方很遠的單，只有利率急漲時才會成交。
- `inherited` 送單前有包絡的利率下限：max(`min_rate_apr`／365, 即時 bid 中位數 × `rate_floor_ratio`)（[2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D1）。它比 E2 晚兩個半月落地，E2 定下 15% 下限時還沒有它。
- `inherited` 單筆上限 200、只放 2 天期（目前的包絡）：一張單掛低的損失上限是該筆 2 天的利息差。

## Options Considered

- **基準. 掛在最低賣價附近，另設最低利率**：開源放貸 bot 的做法（[Poloniex lending bot 設定文件](https://poloniexlendingbot.readthedocs.io/en/latest/configuration.html)：依 book 掛單，另有最低日利率設定）。
- **A（選用）. 拿掉對訊號利率的下限**：一律掛「最低賣價減一檔」，下限只靠包絡。
- **B. 保留下限，參考價改用即時 book 的中位數**。
- **C. 維持現狀**，等訊號每小時更新、E1 每 30 分鐘撤單。

## Decision

- **D1 = A**：刪除 `signal_floor` 分支與 `BFX_BOOK_MAX_DOWN_PCT`。book 的價低於訊號利率時一律掛「最低賣價減一檔」（`raise` 分支）。taker 與 undercut 分支不變。
- **D2**：利率下限由包絡負責，不在定價裡另設。
- **D3**：safety guard（含包絡的利率下限）改成檢查「實際要送出的利率」，在定價之後、送單準備之前評估。原本它檢查的是訊號利率，定價後的利率要到 command gate 內才第一次碰到包絡，被擋時是以例外丟出、每個週期記一次 `deployment_submit_error`。
- **D4**：利率不高於一個 tick 的賣單不當作可跟隨的價（減一檔後不是正利率）；沒有其他夠深的賣單時照 `insufficient_period_depth` 不送單。

## Rationale

- **D1 vs 基準**：A 就是基準。原本偏離基準的理由（防止用舊收盤價賤賣）在包絡的利率下限上線後已由另一個機制承擔，而且那個下限用的是即時 bid，不是一小時前的收盤價。
- **E2 D2 當時的不對稱論證**是「掛太高最壞只是閒置 30–60 分鐘、可逆；掛太低最壞是以爛利率鎖 2 天、不可逆，超過 15% 的下移交給下一個整點的訊號層判斷」。兩個前提現在都不成立：閒置不是偶發的 30–60 分鐘（a30 cell 的訊號利率和 2 天期 book 的落差是常態，每個整點都可能重現）；「以爛利率鎖 2 天」的下界已經由包絡用即時 bid 擋住。
- **不選 B**：多一個參考價與一個比例要調；book 被少數大單扭曲時參考價會偏。包絡已經用 bid 中位數做同一件事。
- **不選 C**：實測三張單 3／3 掛在成交不了的位置。訊號利率是 a30 聚合序列的收盤價時，和 2 天期 book 之間本來就可能有很大的落差，不是偶發。
- **單一低價賣單能把我們拉多低**：同一期限的 book 不會交叉停留——賣價低於同期限最高買價的單會被 venue 立刻撮合掉（offer 期限 ≥ bid 期限即可成交，2 天期是最短期限，所以 2 天期賣單只對 2 天期買單）。所以只要 book 上有該期限的買單，最低賣價就不低於最高買價，「減一檔」最多落在買賣價差之內；沒有買單時包絡的下限取不到 bid 中位數，照規則不送單。2026-11-21 的 book 是買 0.0001301（約 24 萬 UST）、賣 0.0001334。這是依撮合規則的推論，沒有另外做實驗。
- **代價**：利率瞬間下探時，新單會跟著掛低，直到包絡的下限擋住。以目前的單筆上限與期限，單次損失是 200 UST 兩天的利息差。

## Result

- `backend/src/bfx_funding_bot/modules/execution/deployment/period_pricing.py` 刪除 `PriceBranch.SIGNAL_FLOOR` 與 `PeriodPricer.max_down_pct`；`apps/config.py`、`apps/runtime/config.py`、`apps/bot.py`、`deploy/vm/live.env` 與兩份 `.env.example` 移除 `BFX_BOOK_MAX_DOWN_PCT`。
- 回歸測試 `test_offer_follows_a_book_at_less_than_half_the_hourly_signal`（用 2026-11-21 的實際數字）在改動前失敗、改動後通過。
- 每個 release 帶自己的 `live.env`（取自該 revision），回滾到舊版時舊版讀得到它需要的設定。

## Followup

- E1 撤單重掛的參考價用同一個 `PeriodPricer`，所以已掛在高處的舊單在滿 30 分鐘後會被撤掉、以 book 價重掛，不需要另外處理。
- `optimizer_live` 路徑的 `RateOptimizer` 仍會排除低於訊號利率的候選（[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) D6）；該路徑未啟用，啟用前要一起重看。
- taker 分支仍掛訊號利率：bid 高於訊號利率時是否該掛更高，未評估。

## Revocation Triggers

- 包絡的利率下限被移除或放寬到不再以即時 bid 為基準 → 定價要重新有自己的下限。
- 實測出現新單因跟著 book 掛在明顯偏離成交價的低位（例如 book 最低賣價被單一掛單拉低），且金額影響超過單筆上限的設計假設 → 改採 B。
