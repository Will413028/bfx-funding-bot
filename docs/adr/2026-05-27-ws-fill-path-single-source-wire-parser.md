---
title: Bitfinex funding-offer wire 單一 parser + captured-fixture 雙層 contract test，foc EXECUTED 為 WS 成交訊號
date: 2026-05-27
status: active
tags: [bfx-funding-bot, decision, bitfinex, websocket, testing, reconciliation]
related-commits:
  - "3bcfe4c..02ac3d7"
  - "181bf55..4ebe80a"
---

# Bitfinex funding-offer wire 單一 parser、captured-fixture 雙層 contract test、foc EXECUTED 為 WS 成交訊號

## Context

Prior state：4.4a（[2026-05-23-phase4.4a-bitfinex-live](2026-05-23-phase4.4a-bitfinex-live.md) D3）因沒有 testnet，WS fixture 從 Bitfinex docs 手抄。2026-05-25 切 live 前，boot reconcile 的 signed `POST /v2/auth/r/funding/offers/{symbol}` 只跑過 stub；repo 已有兩次 wire bug（APL dot-notation、columnar union null-bleed）都是手寫 mock 資料本身就帶錯假設。2026-05-26 first real money 事故（見 [2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md)）證實同一類：`auth_ws._parse_foc` 用 `d[7]/d[11]/d[12]`，真實 funding-offer array 是 `[10]=status [14]=rate [15]=period`，`float(None)` 讓整個 `foc` 被丟；手寫 `foc_*.json` 照錯索引做，單元測試全綠。結構性原因：**同一個 funding-offer array 在 REST 與 WS 兩處各自解析、索引假設分歧**。另外 `fcn` 的 `offer_id_meta=d[14]` 其實是 `mts_opening`，credit 事件本來就不帶原 offer id。

**約束**：

- `external` Bitfinex funding 沒有 sandbox／paper（paper 只複製 TESTBTC/TESTUSD 現貨），live 驗證只能打真帳戶。
- `external` Bitfinex v2 是 positional array，欄位靠索引；funding credit 事件不帶原始 offer id（官方 layout）。
- `external` CI 不可依賴第三方可用性與 secrets（flaky、不可重現）。
- `inherited` 週期 REST reconcile 已是正確性骨幹（2026-05-27 Plan 1）；現在仍成立的理由：`backend/ARCHITECTURE.md` §3c 仍定義 WS 為 delta 最佳化、REST reconcile 絕對覆寫。所以 WS 路徑壞掉只退化成 ≤90s 延遲，不丟錢。
- `inherited` repo 的 `integration` marker 為「需網路／live」且預設排除；仍成立：commit gate 仍是 `-m "not integration"`。

## Options Considered

### 決策一：怎麼驗證 wire 格式（2026-05-25）

- **基準. 雙層 contract test：錄下真實回應當 golden（VCR／snapshot）跑每個 commit + 另一層 gated live contract 偵測 venue drift**：業界 external integration 測試做法（Martin Fowler「[ContractTest](https://martinfowler.com/bliki/ContractTest.html)」、[vcrpy](https://vcrpy.readthedocs.io/) record-and-replay）。**選用**。
- **A. 手寫 mock 資料（照 docs）**：既有做法；已兩次證明 mock 會把錯假設一起寫進去。
- **B. 預設測試套件直接打 live API**：最真，但 flaky（網路、rate limit、帳戶狀態）、CI 要放 secrets、不可重現。
- **C. 另寫一支 bespoke 驗證 script**：脫離 pytest 慣例，不會持續守門。

### 決策二：怎麼修 WS `foc` parser（2026-05-27）

- **A. 只改 `_parse_foc` 索引（Plan 1 spec 原句）**：最小，但兩份 parser 仍會在下次加欄位時再分歧。
- **B. 單一 wire layout SoT（選用）**：中性模組 `external/bitfinex/funding_offer_row.py` 持有唯一一份索引，REST `parse_active_funding_offers` 與 WS `_parse_foc` 都由 `FundingOfferRow` 建構；strict 於 `venue_offer_id/symbol/status/amount`，`rate/period` tolerant None-guard。放第三模組避免 `auth_rest`↔`auth_ws` import 方向加深。

### 決策三：WS 成交訊號與 OOO staging buffer

- **A. 維持 `fcn` 當成交訊號 + OOO staging buffer**：`fcn` 無 offer id，映射不可能正確；buffer 只為這條錯誤映射存在。
- **B. `foc EXECUTED` 為成交訊號、`fcn` 降為 informational、刪 OOO staging buffer（選用）**。
- **C. 啟用 `RestPollingFillTracker` 當快速偵測層**：多一條 poll 路徑，延後不做。

## Decision

- **D1 = 05-25 spec**：選基準（captured golden `funding_offers_real.json` 跑 default gate + `@pytest.mark.integration` gated live test，`BFX_UPDATE_FIXTURE=1` 或 fixture 不存在時寫入、否則只比結構不比值），而非 A/B/C。為此把 `get_active_funding_offers` 拆出 `fetch_funding_offers_raw`，live test 仍走真 HMAC 簽章。
- **D2 = 05-27 spec §1**：選 B 單一 row parser，而非只修索引。
- **D3 = 05-27 spec §2–3**：`foc EXECUTED`（substring match 狀態字串，如 `EXECUTED @ 0.0005 (100.0)`）→ `OrderFilled(credit_id=None)`；`CANCELED/EXPIRED` → `ReservationReleased` 不變；`fcn` 移除 `offer_id_meta`、dispatcher 對 `fcn` no-op。
- **D4 = 05-27 spec §4**：刪 `_staging_buffer`／`OOO_STAGING_TTL_MS`；unknown voi 的 `foc` 只記 info 並丟棄，交給 reconcile 收斂；`recent_cancels`（user_cancel 分類）保留。
- **D5 = 05-27 spec §5**：WS fixture 一律 capture-and-replay，不再手寫；gated live WS test 以連線即送的 `fos` snapshot（同 layout）零成本證明 WS≡REST layout，`foc EXECUTED` golden 等 canary 自然成交時再錄。

## Rationale

- **D1**：不選 A，因為錯的假設會同時寫進程式與 mock，測試永遠綠；不選 B，因為 CI 必須 deterministic 且不帶 secrets；不選 C，因為守門要常駐在 commit gate。代價：live 層只能人工帶 key 觸發，drift 偵測不是自動的。
- **D2**：相較只修索引，單一 SoT 讓兩條路徑在結構上無法再分歧，一份 golden 同時驗兩邊；代價是多一個模組與一次 behaviour-preserving 重構（以既有 REST golden 當回歸）。
- **D3**：`foc` 帶 `venue_offer_id`，是唯一能不靠猜就對應回 offer 的訊號；`OrderFilled.credit_id` 本就 `str | None`，ledger 效果不依賴它。不選 `fcn` 映射，因為 venue 不給 offer id，任何映射都是猜。
- **D4**：submit 同步寫 `CLAIMED`，`foc EXECUTED` 只可能在 offer 上線撮合後到，claim-before-foc 結構上成立；buffer 防的 race 只屬於已移除的 `fcn` 映射。有權威 reconciler 時，in-band 重排 buffer 只多狀態、TTL 與潛在 bug 面（`ws_dispatcher_ooo_drop ... _realized may be wrong`）。這推翻了 backbone spec「dispatch/OOO staging 正確、保留」的判斷——前提（`fcn` 映射）已不存在。
- 整體不選「重寫 WS 路徑」：dispatch table 與 dedup 本身正確，且 WS 失敗只退化為 reconcile 延遲。

## Result

- `git log --oneline 3bcfe4c..02ac3d7`（D1：raw 拆分、fast contract、gated live）與 `181bf55..4ebe80a`（D2–D5：shared row parser、REST 委派、`_parse_foc` 修正、`foc EXECUTED→OrderFilled`、`fcn` informational、刪 OOO buffer、gated-live WS layout test）。
- 機制仍在 origin/main：`backend/src/bfx_funding_bot/external/bitfinex/funding_offer_row.py`、`backend/tests/external/bitfinex/test_funding_offers_wire.py`、`test_funding_offer_row.py`、`tests/integration/phase4_4a/test_fcn_informational.py`。
- **未如預期**：`funding_offers_real.json` 在 origin/main 仍是 05-25 的 seed 樣本（id `998001/998002`，只被 `d1d6709` 與改名 commit 碰過），D1 的「真實 capture 取代 seed」從未落地；05-27 spec 稱其為「real captured golden」不成立。

## Followup

- 帶唯讀 key 跑 `cd backend && BFX_UPDATE_FIXTURE=1 uv run pytest -m integration -k funding_offers --capture=no`，把真實回應 commit 成 golden（D1 未完成的一步）。
- funding **credit** array 沒有單一 row parser（spec 明說 credit 是不同 wire 物件、不在 D2 範圍）；2026-09-27 `3689adb` 又修了一次 `fcn/fcc` 的 rate/period/close-time 索引。決定是否比照 D2 抽 `funding_credit_row`（REST credits 與 WS `fcn/fcu/fcc` 共用）。

## Revocation Triggers

- Bitfinex 提供 funding sandbox／testnet → 重評 D1 的 live 層是否改打 sandbox 並排進 CI。
- Bitfinex credit 事件開始帶原始 offer id → 重評 D3（`fcn` 可恢復為成交或 attribution 訊號）。
- reconcile 骨幹被移除或降級 → D4 刪 buffer 的前提失效。

## Related

- 來源（Provenance）：
  - `2026-05-25-venue-reconcile-verify-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-25-venue-reconcile-verify.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-27-ws-foc-parser-fix-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-27-ws-foc-parser-fix.md`（原文已不在 repo，本 ADR 即紀錄）
- [2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md) — 本 ADR 是其 Plan 2（WS 延遲最佳化層）。
- [2026-05-23-phase4.4a-bitfinex-live](2026-05-23-phase4.4a-bitfinex-live.md) — D3 手抄 fixture 的前身決策；本 ADR 的 D1/D5 取代其 fixture 來源。
- [2026-05-22-phase4.4-prework-admin-smoke-test](2026-05-22-phase4.4-prework-admin-smoke-test.md) — R2「contract test 驗 wire-level」的同類規則。
- `backend/ARCHITECTURE.md` §3c Fill Lifecycle（現行行為）。
