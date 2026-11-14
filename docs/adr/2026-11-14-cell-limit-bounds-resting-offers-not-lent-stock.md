---
title: cell 上限只管掛單中的金額，不再把已借出的 credit 歸屬到 cell
date: 2026-11-14
status: active
tags: [bfx-funding-bot, decision, ledger, capital]
---

# cell 上限只管掛單中的金額，不再把已借出的 credit 歸屬到 cell

> owner 於 2026-11-14 核可。「Result」列的是核可當下的改動盤點，實作另依計畫進行，完成後把這段換成實際結果。

## Context

prior state：`cell_limit = max(0, T−R) × max_cell_fraction(0.70)`，`E_cell` = 該 cell 的 offers ＋ 尚未反映的
commitments ＋ **歸屬到該 cell 的 active credits**（`backend/ARCHITECTURE.md` I-CAP、I-CC）。credit 的歸屬在每次
接受觀測時由 `ledger/_internal/basis.py` 推論（trade／carry／recent fill 三條規則），寫進
`accepted_capital_basis_credit_cell`，加總進 `accepted_capital_basis_cell`。

**這個數字的來源**：70% 出自 [2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md) D2
的分配政策（「greedy emptiest-first + 70% 集中度上限」，理由是別讓資金閒置、也別全押單一 cell）；同一份的 D4
寫「per-cell 只用於分配」「不依賴 venue credit→cell 歸屬」，全域上限由 venue 的真實數字守。
[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D1 把它沿用成
applied policy 的「既有 max_cell_fraction 0.70」，
[2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md)
的 basis 再把「歸屬到 cell 的 credit」做成授權用的權威數字。這兩步都沒有為「存量也要壓在 70%」寫新的理由。

**推論為什麼做不準**（`external`）：Bitfinex 的 funding offer 沒有 client id；credit 不帶 offer id；loan 會轉成
新 id、拆分金額的 credit，只有 `MTS_OPENING` 不變；借款人還款時 venue 不說還的是哪一筆。funding trade 帶
`OFFER_ID`，所以「成交當下是誰的」可以精確知道，「之後還剩多少是誰的」不行。

**推論的三個方向都出過問題**：

- 未知的 credit 算進每個 cell：2026-09-27 撤掉（`8978f528`）。T=395.52、一筆 150.77 的 credit 在外時，每個 cell 的
  headroom 掉到 126.09，低於最低掛單額約 150，244.75 閒置到還款為止。
- 同一個 opening 有兩個 cell 成交時，每個 cell 都記整組（現行）：模擬環境多 cell 的 credit 列 9,042 筆，正式環境
  0 筆（2026-11-08 量測，分支 `fix/basis-cell-attribution` 的 ADR 草稿）。兩個 cell 各成交 200 時各記 400。
- 未知的 credit 不算進任何 cell（`fix/basis-cell-attribution` 的修法）：2026-11-08 owner 退回，會低估 cell 曝險。
  同一個分支的「組縮小時各 cell 按比例縮」也會低估：X 有 300、Y 有 100，借款人還掉 Y 的 100，按比例把 X 算成 225。

「不重複計」「不低估」「不閒置」三者在現有資料下無法同時成立。

**現況的 cell**：正式環境兩個，都是 `mean_reversion`、fUST、放 2 天，只差訊號用的 K 線聚合
（`backend/configs/cells.live.yaml`；包絡 ADR Followup 的「period 2/2」）。借出去的錢在哪個 cell 名下，不改變幣別、
天期、對手（venue）任何一項。

**約束**：

- `external` venue 不提供 credit→offer 的連結與還款對應（上段）。
- `inherited` ledger 的 basis 表 insert-once；帶 migration 的 release 不能靠上一版 image 回滾。
- `inherited` 真正的風險上限已在別處且用精確數字：總額與保留（`T`、`R`）、單筆上限、天期範圍、利率下限、掛單數
  （[2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D1）。

## Options Considered

- **基準**：多策略共用一個帳戶時，歸屬在成交當下由自己的 order id 決定並寫死；與交易所總量對不上的差額放進明確的
  「外部」帳本、計入帳戶層上限（NautilusTrader 對帳把無主的單記為 `EXTERNAL` 策略）。需要硬隔離時用子帳戶。風險
  上限掛在看得到的風險因子（幣別、天期、對手）上（最後一句是歸納，未查文件）。
- **A′ 內部分帳＋上界**：每組每 cell 存自己的成交額，授權取 `min(自己的成交額, 該組還在的金額)`。不低估，但兩個
  cell 合計可超過實際金額；小資金時 headroom 會再度低於最低掛單額而閒置（2026-09-27 那個問題的同一型）。
- **B 每個 cell 一個子帳戶**：歸屬由 venue 保證。每個子帳戶都要過最低掛單額，各自一把 key 與一個 worker
  （[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)）；
  以現在的資金規模與兩個同質 cell 不划算。
- **C（採用）cell 上限只管掛單中的金額**：`E_cell` = 該 cell 掛著的 offers ＋ 尚未反映的 commitments，兩者都由 bot
  自己的 provenance 精確決定。已借出的 credit 只計入幣別的總量，不再歸屬到 cell。
- **D 維持現狀**：缺陷照舊（重複計、閒置）。

## Decision

1. **`E_cell` 的定義**改為「該 cell 掛著的受管 offers 的剩餘金額＋尚未被 basis 反映的 commitments」。`cell_limit`
   的公式不變；它的意思變成「一個 cell 同一時間最多能掛多少」，也就是 2026-05-29 D2 的分配規則。
2. **credit 不再歸屬到 cell**。basis 繼續以 venue 的精確數字記每個幣別的 available／offered／credits／外來 offer，
   conservation（`venue_lent_above_ledger`、`foreign_lending`）不變。
3. **刪除的機制**：`basis.py` 的 trade／carry／recent fill 三條歸屬規則與它們的輸入（`traders`、`carried`、
   `prior_cells`、recent fill 的候選）、新 basis 對 `accepted_capital_basis_credit_cell` 的寫入、
   `trades_range_uncovered` 這個 block（它只為「新 opening 要有 trades 才能歸屬」而存在；刪除前確認 conservation
   不依賴它）。分支 `fix/basis-cell-attribution` 不合併。
4. **`unattributed_credits`**：所有 credit 都不歸屬，這個欄位不再有區分力。API 與營運 UI 的
   `unattributed_credit_exposure`／`unattributedLent` 改成不顯示或改名，另列在實作計畫。
5. **報表不受影響**：週報的 per-cell 報酬用自己的歸屬（`modules/live_validation/credit_attribution.py`，以 trade 的
   `OFFER_ID` 對回 cell，對不上記 `unattributed`），報表允許「未知」，授權不允許，所以兩者分開。
6. **之後若 cell 的天期不同**：加天期上限，直接用 credit 自己的 `period`（venue 給的，精確），不經過 cell。

## Rationale

- 存量在哪個 cell 名下，在兩個同幣別、同天期的 cell 之間沒有本金風險的差別；最壞情況是資金全照其中一個訊號的利率
  借出 2 天，而利率由包絡的利率下限把關。
- 授權裡不再有推論出來的數字，「低估」與「重複計」都不會發生；`trades_range_uncovered` 造成的暫停也消失。
- 掛單中的金額是 bot 自己的 offer，provenance 精確，不需要 venue 沒給的資料。

## Result（核可當下的改動盤點，`origin/dev` `1b3b33b5`；尚未實作）

`git grep -lE 'cell_exposure|max_cell_fraction|cell_limit|cell_headroom|accepted_capital_basis_(credit_)?cell|AcceptedCapitalBasis(Credit)?Cell'`
在 `backend/src`、`backend/scripts`、`deploy`、`frontend/src` 命中 13 個檔：

| 位置 | 預計 |
|---|---|
| `modules/ledger/_internal/basis.py`（783 行） | 刪歸屬規則與 credit_cell 寫入；`cells` 只累計 offers |
| `modules/ledger/tables.py`、`modules/ledger/__init__.py` | `accepted_capital_basis_credit_cell` 留表與歷史、不再寫入；`unattributed_credits` 的去向另定 |
| `modules/ledger/_internal/capital_reader.py` | 讀法不變（仍讀 `accepted_capital_basis_cell`），內容少了 credit |
| `modules/trading/policy.py`、`modules/trading/capital.py` | 公式不變；docstring 與 `unattributed_credits` 的驗證調整 |
| `modules/execution/deployment/sizing.py` | 不變（emptiest-first 改以掛單中的金額排序，行為隨 `E_cell` 定義改變） |
| `apps/restore_boot_check.py`、`deploy/vm/pgbackrest/ledger_digest.py` | 仍讀 `accepted_capital_basis_cell`；確認 digest 與 restore gate 新舊方向都過 |
| `modules/api/funding_status.py`、`frontend/src`（3 檔＋`api.gen.ts`） | `cell_limit`／`cell_headroom` 的說明文字；`unattributed` 的顯示 |
| `backend/scripts/capital_mutation_gate.py` | 對應的 mutant 調整 |
| `backend/ARCHITECTURE.md` | I-CAP、I-CC 與「cell exposure 只算可歸屬的部分」一段改寫 |
| migration | basis `schema_version` 升版（canonical payload 不再帶 credit 的 cell 集合）；不刪表 |
| 測試 | `cell_exposure` 35 行／9 檔、`accepted_capital_basis_*cell` 19 行／約 8 檔等，逐一改寫或刪除（刪測試要 `Removed-Test:`） |

未盤點：`fills`（部分成交的證據）除了 recent fill 歸屬之外有沒有其他讀者；模擬環境既有 basis 裡的舊歸屬要不要處理
（正式環境 0 筆）。

## Revocation Triggers

- owner 指出 70% 要防的東西取決於存量在哪個 cell（例如某個訊號的模型風險要以存量計）→ 改採 A′，並接受小資金時的
  閒置。
- 出現天期、幣別或資金來源不同的 cell → 先加以 credit 自身屬性計的上限（Decision 6）；需要硬隔離時走子帳戶。
- Bitfinex 提供 credit 到 offer 的直接連結 → 可以重新評估以存量計的 per-cell 上限。
