# 用小額真錢量 7 天期借方會不會提前還款

**Goal：** bot 在預登記的條件下自動掛 7 天期的 fUST 放貸單（借出中加掛單中合計 ≤ 600 USDt、一次只掛一張），累積到 20 筆已結束的 7 天期借款或滿 10 週後自動停止掛 7 天期；持有時間報告能依天期算出預登記的四個指標，結果依預登記的判讀寫回研究 registry 與 ADR 2026-09-27。驗收見步驟 10。

**上層：** 主清單見本機個人脈絡檔（`AGENTS.local.md`）。本檔暫代主計畫。

**附件：** `2026-11-22-seven-day-tenor-measurement/`（`requirements.md`：需求清單、現況盤點、外部介面；快照 `80fe72c2`）。

**決定紀錄：** owner 2026-11-22 核可實驗設計（D1）。依據是 ADR `docs/adr/2026-09-27-keep-always-2d-over-rate-threshold-tenor.md` 的 D2：「若仍要 live 試，候選只有選項 D（7 天期門檻規則），定位是量測借款人持有行為」。預登記原文在 repo 外的研究 registry。

## 全域約束與完成定義（暫代主計畫）

- 預登記的內容（天期、掛單條件、上限、停止條件、指標、判讀）開跑後不改；要改就停下來重新登記。
- 預設關閉：沒有設定本計畫新增的環境變數，或包絡的 `max_period_days` 低於 7 時，bot 的行為與改動前相同。
- 不靠手動掛單（`AGENTS.md`「放貸全自動」）。人工動作只有發版核准與改包絡這兩種一次性操作。
- 不新增依賴；不新增 suppression（`noqa`、`type: ignore`）。
- 動到真錢路徑的步驟（1、2、3）：先寫會失敗的回歸測試；完成前跑 `/code-review` 與契約審查；mutation 在複本跑。
- 發版（`dev`→`main`）、改包絡、對正式 DB 的唯讀查詢，執行前都要 owner 核准。
- 本機只跑 ruff、mypy、lint-imports、受影響的單元測試與 mutation；全套單元、integration、e2e 以 PR 的 CI 為準。
- 完成定義：需求清單每項有對應的測試或現場證據；新增行為有「破壞新行為要失敗」的測試；`ruff check`、`mypy src/`、`lint-imports`、quality ratchet 綠；CI 綠。

## 前提（開工時核對過，2026-11-22）

- 會改到的既有部署是否在線：**在線**（VM，production `b205806a`，放貸中）。回滾方式：(a) 程式：新的環境變數不設定就不動作，發版回滾＝部署上一版 digest（本計畫沒有 migration）；(b) 啟用：把包絡的 `max_period_days` 改回 2，規則立刻不再選 7 天；(c) **已成交的 7 天期借款收不回來**，只能等到期或借方還款，這是唯一不可逆的部分，上限是 R3 的 600 USDt 鎖 7 天。
- 新單的天期只有一個來源（每小時訊號的 `StandingQuote.period_days`），下游只讀 `DecisionPayload.offer_duration_days`（盤點 1；已確認）。
- 部署層拿得到受管掛單的天期與建立時間，拿不到逐筆借款；借款的天期在 `venue_credit_mirror.period_days`（盤點 2、3；已確認）。
- 撤單重掛（E1）在目前的 `book` 參考模式下不會撤 7 天期掛單（盤點 4；已確認，讀碼）。
- 包絡超出天期範圍時是一般的 skip，不是停機（盤點 5；已確認，讀碼）。
- 改包絡不用發版（盤點 6；已確認，runbook）。
- 一份 book snapshot 同時含 2 天與 7 天期的價位，只要它們在每側 25 檔內（盤點 8；已確認，讀碼）。7 天期可見率 42–61%（2026-07-19..09-27 量測；未重新量）。
- 掛 7 天期的單成交後，借款記的天期是 7 天還是借方要的天期：**未確認**，沒有樣本。由步驟 6 回答，擋住步驟 8 之後。因此第零步的上限不能靠借款的天期來數，要靠自己送出的掛單（見 D5）。
- 正式環境 fUST 的包絡：單筆上限 200、天期 2–2、最多 6 張掛單（`sudo bfx-op status`，2026-11-21 讀；已確認）。近期單筆金額 152.74–200（`funding_trades`，2026-11-21 的 5 筆；已確認）。所以第零步的金額上限 200 容得下一筆單。
- 自己送出的掛單有兩個來源，可靠度不同（已確認，讀碼）：(a) 送單紀錄：command journal 裡被 venue 確認（ack）或綁定到 venue 掛單的 attempt，天期在對應的 execution decision（`duration_days`），受管與否由 `offer_provenance` 判定（`modules/ledger/read/provenance.py:28-68`）；送單當下就寫入。(b) 掛單鏡像 `venue_offer_mirror`：只在掛單出現於某次被接受的觀測快照時才建列（`modules/ledger/_internal/observation.py:455-469`），掛出後在下一次觀測前就全部成交的單沒有列；結束時也不回寫最後的金額（同檔 `:424-434`）。所以累計張數以 (a) 為底；「確定沒成交」只認 venue 回報的掛單終態（掛單歷史列），不讀鏡像表的金額。
- 「當下 2 天期利率」與「7 天期的價」都用同一個定價器（`PeriodPricer`）對同一份 book、同一筆金額定出來的實際送單價，不另寫一套判定。兩邊都是「實際會送出的利率」，所以比較是對稱的。預設（可推翻）；ADR 原文只寫「當前 2d rate」。
- 沒成交的 7 天期掛單，掛滿 1800 秒就撤回（與 E1 的最短掛單時間 `BFX_REPRICE_MIN_AGE_S` 相同）。預設（可推翻）。
- 自動停機（`HALTED/auto`）發生時，既有告警會通知 owner；本計畫不自動判定「是不是實驗引發的」，由 owner 看告警後決定要不要把包絡改回 2–2（寫進 runbook）。預設（可推翻）；需求 R5 的後半句以這個方式處理。
- 實驗期間每週報告與 G3 的 cell 歸屬會包含 7 天期借款（`modules/live_validation/` 依 cell 彙總，讀借款的 `period_days`）。不另外排除：金額上限 600、期間 10 週；讀那段期間的 G3 時要註明。預設（可推翻）。
- 實驗的運行開關是包絡的天期上限：程式發版後，包絡改成 2–7 才生效，改回 2–2 就停，不用再發版。筆數上限、利率門檻、截止日這些參數放環境變數，改它們要發版。預設（可推翻）。

**驗收基線：** `dev` 的 `80fe72c2` 在 PR #457 與發版 PR #458 的 CI 全綠（`gate (ci)`、`gate (security)`、`gate (capital-rules)`，含全套單元、integration、e2e）。本機全套單元測試在這台機器上可能因記憶體不足被系統中止，不作為基線。

## 本階段依賴的不變量

| 不變量 | 依賴它的機制 |
|---|---|
| 送單的唯一寫入者是 `DeploymentReconciler`，撤單一律經 command gate | `execution/deployment/reconciler.py`、`execution/command_gate.py`（`cancel`、`_guard`） |
| 每筆新單送出前，包絡以實際要送出的天期與利率檢查 | `execution/safety/pre_trade.py` 的 `OfferEnvelopeGuard`；`reconciler.py` 定價後的 `self._safety.evaluate` |
| cell 上限只算掛單中的金額，不算已借出的 | `trading/policy.py` 的 `cell_limit`／`cell_headroom`；ADR 2026-11-14。步驟 1 新增的天期上限是另一條獨立的限制，不改這條 |
| 借款的天期以 venue 給的為準 | `ledger/tables.py` 的 `venue_credit_mirror.period_days`；步驟 1 的讀取、步驟 6 的判讀 |
| 掛出去的長天期單，實際送出的利率不低於預登記的門檻 max(0.0002／日, 當下 2 天期利率) | 步驟 2：以定價後的利率判斷，不是以 book 上的賣價判斷 |
| 第零步的「累計 1 張」不依賴借款記的天期 | 步驟 1：累計以送單紀錄計（被 venue 確認或綁定的 attempt），不以 `venue_credit_mirror.period_days` 計，也不以掛單鏡像表有沒有列計 |
| 同一個 tick 內送出的長天期單會立刻計入上限 | 步驟 2：tick 內累加，含送出結果不明（UNKNOWN）的 |
| 沒有設定新環境變數時行為不變 | 步驟 2 的政策物件預設為 `None`（比照 `ladder_policy_from_env`） |
| live 真錢路徑不讀研究產物 | 本計畫的報告（步驟 7）只在研究用的一次性容器跑 |

## 範圍

- 做：帳本提供長天期曝險的讀取；部署層的天期選擇規則與上限；7 天期掛單的逾時撤回；持有時間報告依天期分組並補兩個指標；發版、改包絡、第零步、累積樣本、判讀。
- 不做：14、30 天期（可見率 5–10%，湊不到樣本）；fUSD（未啟用）；AdaptivePeriod 的啟用（它的 14 天期規則另有待辦）；把報告接進每週工作（樣本累積慢，手動跑兩三次即可）；`optimizer_live` 路徑。
- 延後：
  - 實驗判讀為「值得做」之後的正式長天期策略（觸發：步驟 9 的判讀結果是值得做）。
  - 「掛單利率遠離 book」的告警（觸發：下次動告警或 execution audit）。

## 步驟

- [x] **1. 帳本提供長天期曝險的讀取**（被擋於：無）——拆成兩個子步驟：1a 純計數、1b 資料庫讀取與接線，皆完成（2026-11-22，PR #460）；索引題待 Will，見 D7
  - 檔案：修改 `backend/src/bfx_funding_bot/modules/ledger/__init__.py`（回傳型別與讀取介面）、`modules/ledger/` 下實作讀取的檔案（修改；開工時以 `rg -n "managed_live_offers" backend/src/bfx_funding_bot/modules/ledger` 找到既有讀取的位置，新讀取放同一檔）、`modules/execution/deployment_input.py`（修改：新輸入，並把受管掛單的 `signal_correlation_id` 帶進 `venue_offers` 的元素）、`external/bitfinex/auth_rest.py`（修改：`ActiveFundingOffer` 新增選填欄位 `signal_correlation_id`，預設 `None`）、`modules/execution/periodic_reconcile.py`（修改）、`modules/execution/deployment/reconciler.py`（修改，`deploy` 多收一個輸入）、`apps/bot_ports.py`（修改）、`apps/bot.py`（修改）、`backend/ARCHITECTURE.md`（修改）；新增 `backend/tests/modules/ledger/test_long_tenor_exposure.py` 與對應的 integration 測試。
  - 範圍：部署層每個 tick 拿到一份「指定幣別、指定天期」的曝險，四個數字，各自的來源寫死：
    1. **累計成交過的長天期掛單數**：以送單紀錄為底——本帳戶的 attempt（`submission_attempt_journal`），被 venue 確認（`transport_outcome_journal.kind='ack'`）或綁定到 venue 掛單（`execution_resolution_journal.action='bound_to_venue'`），幣別相符、天期（對應 execution decision 的 `duration_days`）等於該天期、`started_at_ms` 不早於 `BFX_LONG_TENOR_SINCE`。從中扣掉「確定沒成交就結束」的，四個條件要全部成立：(i) 該掛單在 `ledger_observation_offer_history` 有列（venue 回報的終態）；(ii) 那一列的 `terminal_kind` 是 `canceled`、`status` 不是部分成交、`amount_original` 與 `amount_remaining` 都有值並相等；(iii) `ledger_observation_trade` 裡沒有這張掛單的成交列；(iv) 金額一律讀歷史列，不讀 `venue_offer_mirror`。其餘一律算一張：還在掛、全部成交、部分成交、金額欄是 NULL、還沒有歷史列。還沒有送出結果（沒有 transport outcome 列：送出後、寫 outcome 前中斷）的 attempt 也算一張（單可能已在 venue 上；2026-11-22 1b 契約審查後補登）。送出結果不明（UNKNOWN）的 attempt 在未解期間算一張，解成 `bound_to_venue` 後照上面計，解成沒掛出（`not_accepted`）的不計。這個數字不看借款，也不依賴掛單有沒有被觀測到。
    2. **掛單中的長天期金額與張數**：既有 `venue_offers` 裡該天期的受管掛單（不新增查詢）。
    3. **借出中的長天期金額**：`venue_credit_mirror` 裡 `present_in_latest_accepted_snapshot` 為真、`period_days` 等於該天期或為 NULL（D8）、`mts_opening` 不早於 `BFX_LONG_TENOR_SINCE` 或為 NULL（步驟 3b）的列的金額合計（loan 與 credit 兩種都算：同一時刻一筆放貸只會是其中一種）。這個數字在 D3＝A（借款記 7 天）成立時才有意義。
    4. **已結束的長天期借款筆數**：`venue_credit_mirror` 裡 `source_kind='credit'`、`period_days` 等於該天期、`mts_opening` 不早於 `BFX_LONG_TENOR_SINCE`、已不在最新快照的列數（只數 credit，與報告判讀用的是同一個定義、不同的表（報告讀 `funding_credit_history`），步驟 9 判讀時核對兩邊筆數；loan 列不數，避免同一筆放貸先 loan 後 credit 被數兩次）。
    只讀，不寫任何表。四個數字的查詢都以幣別、天期與建立時間不早於 `BFX_LONG_TENOR_SINCE` 限定範圍，不掃全部歷史。過濾與計數寫成不碰 DB 的純函式（輸入是列的清單），DB 查詢只負責把列取出來。這是盤點 9 那個延後項（以 credit 自己的 `period` 計的天期上限）的讀取面。
  - 消費端：`rg -n "deployment_input|\.deploy\(|venue_offers=" backend/src backend/tests`。
  - 不能動：`venue_credit_mirror`、`venue_offer_mirror` 的寫入端；cell 上限的算法；capital 的授權邏輯。
  - 驗收：純函式的單元測試（只數指定天期與幣別；確定沒成交就被撤的長天期掛單不計入累計；部分成交後被撤的計入；**從未出現在任何觀測快照就全部成交的掛單要計入**；金額欄是 NULL 或還沒有歷史列的計入；**鏡像表顯示已撤回且金額相等、但歷史列金額不等或有成交列的計入**；歷史列的狀態是部分成交的計入；UNKNOWN 未解時計入、解成沒掛出後不計；早於啟用時間的不計；`period_days` 為 NULL 的列不算進已結束筆數，仍在借出中的算進借出中金額（D8，2026-11-22 改）；已結束的借款不算進借出中；loan 列不算進已結束筆數）；mutation 在複本對純函式跑：拿掉天期條件、把「確定沒成交」的也算進累計、把累計改回只數鏡像表有列的掛單、把 NULL 當成沒成交、金額改讀鏡像表、拿掉「沒有成交列」的條件、把已結束的算進借出中、把 loan 也算進已結束筆數，各自要讓單元測試失敗。DB 查詢本身由 integration 測試覆蓋，在 CI 跑。
  - 停止條件：送單紀錄取不到該筆單的天期，或分不出受管與否 → 回報，不退回只用鏡像表。某張長天期掛單超過一小時仍沒有歷史列（venue 查不到終態）→ 回報 owner；它在這段時間算一張，第零步會停在那裡。
  - 子步驟：**1a** 純計數：`backend/src/bfx_funding_bot/modules/ledger/read/long_tenor.py`（新增；`long_tenor_exposure` 與輸入、輸出型別）與 `backend/tests/modules/ledger/test_long_tenor_exposure.py`（新增）。**1b** 資料庫讀取與接線：把送單紀錄（含對應的掛單終態與有無成交列）與 `venue_credit_mirror` 的列讀成 1a 的輸入型別；經 ledger 的 facade 匯出；`deployment_input`、`periodic_reconcile`、`reconciler.deploy` 接線（`bot_ports`、`bot` 的接線移到步驟 2：查詢的天期與啟用時間來自步驟 2 新增的環境變數，1b 沒有來源可傳；2026-11-22 1b 契約審查）；`venue_offers` 的元素帶上 `signal_correlation_id`；integration 測試；`ARCHITECTURE.md`。1b 的驗收是本步驗收裡 1a 以外的全部。
  - 完成後的狀態：多一個沒有人使用的輸入，行為不變。

- [x] **2. 天期選擇規則（預設關閉）**（被擋於：步驟 1）——完成（2026-11-22，PR #460），見進度表
  - 檔案：`apps/bot_ports.py` 把政策的幣別、天期、啟用時間組成 `LongTenorQuery` 傳給 `LedgerDeploymentInput(long_tenor=...)`（自步驟 1b 移入）；新增 `backend/src/bfx_funding_bot/modules/execution/deployment/tenor.py`（政策物件、從環境變數建立、純函式的選擇規則）與 `backend/tests/modules/execution/deployment/test_tenor.py`；修改 `deployment/reconciler.py`、其測試、`apps/bot.py`、`backend/ARCHITECTURE.md`、`.env.example`、`backend/.env.example`。
  - 範圍：部署迴圈對每一筆要送的單，在既有的定價之前多做一次判斷。依序檢查，任何一條不成立就維持訊號給的天期，照既有流程走：
    1. 政策物件存在（環境變數有設）、這筆單的幣別等於政策的幣別、且該幣別包絡的 `max_period_days` ≥ 長天期（包絡取自這筆單所屬 cell 的 capital view，也就是迴圈裡已經讀到的 `views[cell_id].applied.policy`）；
    2. 現在時間早於截止日（`BFX_LONG_TENOR_UNTIL`，`YYYY-MM-DD`，UTC，該日 00:00 起不再掛）；
    3. 累計成交過的長天期掛單數（步驟 1 的第 1 個數字）加上本 tick 已送出的張數，低於總上限（掛單中的單不另外加：步驟 1 的計數只扣掉 venue 證明沒成交就結束的單，掛單中的已經算在裡面；2026-11-22 步驟 2 契約審查）；曝險讀不到（`None`）算這條不成立；
    4. 掛單中的長天期張數加上本 tick 已送出的張數，低於同時掛單上限（固定 1：一次只掛一張長天期單，成交或逾時撤回後才掛下一張）。掛單中的張數與金額在讀曝險的同一個交易裡，從帳本的受管掛單算（`LongTenorState.resting`）：沒觀測到利率的也算，天期不明或 provenance 矛盾的也算（fail closed）；不用 `venue_offers`，它為了撤單重掛丟掉沒有利率的列（2026-11-22 步驟 2 `/code-review`）；
    5. 借出中的長天期金額＋掛單中的長天期金額＋本 tick 已送出的長天期金額＋這筆單的金額，不超過金額上限；已結束的長天期借款筆數（步驟 1 的第 4 個數字）低於樣本上限；
    6. 把這筆單的天期改成長天期後，用既有的 `PeriodPricer` 對同一份 book 定價得到一個價（被擋就算不成立）；同一筆單以 2 天期定價也得到一個價（被擋也算不成立）；長天期的價 ≥ max(利率門檻, 2 天期的價)。

    全部成立後，先用 safety chain 判一次長天期版本（包絡的底價與天期檢查依天期不同）；被擋就在同一個 tick 退回訊號的天期，evidence 記第 7 條 `guard`（2026-11-22 步驟 2 `/code-review`：否則該 cell 每個 tick 都閒置）。通過才以長天期的 `DecisionPayload` 與剛才定出的價往下走；之後的 safety guard、execution gate、送單沿用既有路徑。本 tick 送出長天期單後（結果是 UNKNOWN 也算），立刻計入第 3–5 條的「本 tick 已送出」。每次判斷的結果寫進 execution decision 的 evidence：沿用 reconciler 傳給 `ExecutionGate.prepare` 的 `optimizer_evidence` 字典，新增鍵 `tenor`（選了哪個天期、第幾條不成立、兩個天期定出的價），落在 `execution_decisions.model_evidence->'optimizer'->'tenor'`（`prepare` 把 `optimizer_evidence` 放在 `optimizer` 鍵下）；沒有政策或幣別不符時不寫這個鍵。`ExecutionGate.prepare` 的簽章不改。
    開機條件：政策只在 `BFX_EXECUTION_POLICY=book_guarded`（optimizer 的成交模型是 cell 自己天期的）且撤單重掛為 `BFX_REPRICE_REFERENCE=book`（quote 模式會拿 2 天期的 quote 把長天期掛單當 stale-high 撤掉）時允許；正式環境兩者都符合（`deploy/vm/live.env`）。
    環境變數（全都沒設＝政策為 `None`；只設一部分、值不合法、或幣別不在 cells 裡＝開機失敗，避免實驗半設定卻安靜地沒開；2026-11-22 步驟 2 `/code-review`）：`BFX_LONG_TENOR_SYMBOL`、`BFX_LONG_TENOR_DAYS`、`BFX_LONG_TENOR_MIN_DAILY_RATE`、`BFX_LONG_TENOR_MAX_AMOUNT`、`BFX_LONG_TENOR_MAX_OFFERS_TOTAL`（累計成交過的掛單上限）、`BFX_LONG_TENOR_MAX_ENDED_CREDITS`（已結束借款的樣本上限）、`BFX_LONG_TENOR_SINCE`（`YYYY-MM-DD`，UTC；只數這一天 00:00 之後建立的掛單與借款）、`BFX_LONG_TENOR_UNTIL`、`BFX_LONG_TENOR_OFFER_TIMEOUT_S`。
  - 消費端：`rg -n "ladder_policy_from_env|policy_from_env|DeploymentReconciler\(" backend/src backend/tests`（新政策物件的接線位置）；`rg -n "offer_duration_days" backend/src`（天期的讀取端，本步不改它們）；`rg -n "period_days" backend/src/bfx_funding_bot/modules/live_validation backend/scripts`（讀借款天期並依 cell 彙總的下游：週報與 G3，本步不改，處置見前提段）。
  - 不能動：包絡 guard 的檢查；`PeriodPricer` 的定價規則；每小時訊號與 `StandingQuote`；command gate；`ExecutionGate.prepare` 的簽章。
  - 驗收：單元測試逐條覆蓋六個條件各自不成立時維持 2 天、全部成立時選長天期；兩個 cell 同一個 tick 都有單、同時掛單上限 1 時只有一筆是長天期；book 上有 7 天期借方出價 ≥ 訊號利率、而訊號利率低於門檻時不掛長天期（taker 分支）；2 天期定不出價時維持 2 天；政策為 `None` 時既有的 reconciler 測試不改就全過；evidence 的 `tenor` 內容。mutation：六個條件各拿掉一個、拿掉 tick 內累加、把第 6 條改成只比 book 上的賣價、截止日比較反向、金額檢查不含這筆單，各自要讓測試失敗。
  - 停止條件：定價路徑對長天期的 `DecisionPayload` 有任何地方仍假設 2 天（例如 fill evidence 的 `period_agg`）而讓單被擋或被錯誤定價 → 回報，不繞過。
  - 完成後的狀態：必須與步驟 3 同一次發版；單獨發版時長天期掛單沒成交會一直掛著。

- [x] **3. 沒成交的長天期掛單逾時撤回，以及包絡不允許時撤回**（被擋於：步驟 2）
  - 檔案：修改 `deployment/tenor.py`、`deployment/reconciler.py`、`deployment/reprice.py`（預設撤單額度抽成常數）、三者的測試、`backend/ARCHITECTURE.md`、`docs/runbooks/operations.md`（新增一段「7 天期量測實驗：啟用、停止、回滾的順序」）。
  - 範圍：每個 tick，政策存在時檢查該幣別受管掛單裡天期等於長天期的：(a) 掛滿 `BFX_LONG_TENOR_OFFER_TIMEOUT_S` 秒的，或 (b) 該幣別包絡的 `max_period_days` 已低於長天期的，經既有的撤單路徑（`self._canceller.cancel`，guard 與 E1 相同）撤掉。位置：放在讀 capital 之前、而且在 `if self._reprice is not None and venue_offers` 這個條件之外，所以不看 `BFX_REPRICE_ENABLED`、不需要 active quote、capital 讀取失敗時也照跑。包絡的來源：(b) 需要的包絡用 reconciler 已有的 `self._capital.read_policy`（不經過 cell 的 capital view，`_pull_if_stopped` 已在用）另讀一次該幣別目前套用的 policy；讀不到時只做 (a) 並記一行 log，不撤也不擋後面的流程。`signal_correlation_id` 傳步驟 1 帶進 `venue_offers` 的那個值，由字串轉成 `UUID`（command gate 之後會以自己的 provenance 為準）；值是 `None` 的掛單不撤並記 log。每 tick 的撤單額度與 E1 共用同一個數字：把 `deployment/reprice.py` 裡的預設值 3 抽成常數，reprice 為 `None` 時用它；長天期的撤回先用額度，E1 用剩下的。政策為 `None` 時不做任何事；帳戶停止或幣別停用時的整批撤單不受影響。
  - 消費端：`rg -n "_reprice_sweep|max_cancels_per_tick|_canceller|cancel_budget" backend/src backend/tests`。
  - 不能動：E1 的判斷規則；撤單的 guard。
  - 驗收：單元測試（未滿逾時不撤、2 天期掛單不撤、包絡改回 2–2 後未滿逾時也撤、撤單額度用完就停且 E1 拿到剩下的額度、`BFX_REPRICE_ENABLED=false` 時照撤、沒有 active quote 時照撤、capital 讀取失敗時照撤、讀不到包絡時只做逾時撤回、`signal_correlation_id` 為 `None` 的掛單不撤、reprice 為 `None` 時額度是 3、撤單失敗不影響同 tick 其他流程、政策為 `None` 時不撤）；mutation：拿掉天期條件（會撤到 2 天期的單）、拿掉逾時條件、拿掉包絡條件、不扣撤單額度，各自要讓測試失敗。runbook 的那一段照字面可執行（指令與既有章節一致）。
  - 停止條件：同一筆資金在一個 tick 內被撤又被掛（沒有經過 venue 釋回）→ 回報。每 30 分鐘撤一次再重掛是預期行為，不是停止條件。
  - 完成後的狀態：回滾的順序固定為：先把包絡改回 2–2 → 等長天期掛單被撤光（`sudo bfx-op status` 與 bot log 確認沒有長天期掛單）→ 才回滾程式或拿掉環境變數。順序反了會留下沒有人撤的長天期掛單。

  - 實作與原文的差異（2026-11-22 審查後）：撤回放在逐幣別迴圈**之前**（原文寫「讀 capital 之前」；放在迴圈裡時，排在前面的幣別的 E1 會先用光共用額度，長天期就不是先用）；`signal_correlation_id` 取帳本受管掛單自己的（`ManagedOffer.signal_correlation_id` 型別是非空字串，所以原文的「值是 `None`」改成「轉不成 UUID 的不撤並記 log」）；逾時比較加上 `VENUE_CLOCK_TOLERANCE_MS`（`.claude/rules/bitfinex-venue.md`）；曝險讀取失敗只擋新單，撤單照跑（曝險改在同一交易的 savepoint 裡讀）。

- [x] **3b. 發版前的三項修補**（被擋於：無；擋住步驟 4）——完成（2026-11-22，PR #460），見進度表。步驟 3 的 `/code-review`（2026-11-22）找到、屬於步驟 1、2 的程式：
  - (a) 長天期被 guard 擋下、退回原天期時，`_guard_long` 走完整的 `self._safety.evaluate`，每次都發 `safety_trigger` warn 事件；包絡底價依天期不同時約每 90 秒一次，會淹掉真正的擋單。改成試算不發事件（或只在原天期也被擋時發），附測試。
  - (b) 借出中金額的讀取以 `mts_opening >= since_ms` 過濾，`mts_opening` 為 NULL 的借款被丟掉，金額上限不再 fail closed。改成 NULL 也算進借出中，附 integration 測試。
  - (c) `deploy` 仍帶 baseline 的 `# noqa: C901, PLR0912, PLR0915`，步驟 1b 說併入步驟 2 拆，沒拆。把每筆單的天期與定價選擇抽成 helper、調低 ratchet（`AGENTS.md`「碰到 baseline 的函式時順手拆掉」）。
  - 驗收：各自有「改回去要失敗」的測試；ruff、mypy、lint-imports、ratchet 綠；`/code-review`。
  - 實作與原文的差異（2026-11-22）：(a) 試算改用 safety chain 既有的 `dry_evaluate`（不發事件、不記 heartbeat、不短路）；guard 逾時或拋例外時 evidence 記 `guard_error` 並寫 error log，不發 `critical` 事件。(b) `mts_opening` 為 NULL 的列只算進借出中，不算進已結束筆數（樣本要確定落在實驗期間）。現行的觀測驗證拒收沒有 `mts_opening` 的借款（`ledger/_internal/observation.py` 的 `invalid credit or loan`），所以這條是防線，integration 測試直接把列改成 NULL。(c) `deploy` 拆成逐幣別、逐筆單的 helper，ratchet 的 `noqa` 745 → 744；搬進 `_plan_symbol` 的 `# noqa: BLE001` 是原有的，沒有新增。

- [x] **4. 發版（第零步的設定）**（被擋於：步驟 2、3、3b；owner 核准）
  - 檔案：修改 `deploy/vm/live.env`。
  - 範圍：`live.env` 設定 `BFX_LONG_TENOR_SYMBOL=fUST`、`BFX_LONG_TENOR_DAYS=7`、`BFX_LONG_TENOR_MIN_DAILY_RATE=0.0002`、`BFX_LONG_TENOR_MAX_AMOUNT=200`、`BFX_LONG_TENOR_MAX_OFFERS_TOTAL=1`、`BFX_LONG_TENOR_MAX_ENDED_CREDITS=20`、`BFX_LONG_TENOR_SINCE=<步驟 4 發版日>`、`BFX_LONG_TENOR_UNTIL=<步驟 4 發版日＋14 天>`、`BFX_LONG_TENOR_OFFER_TIMEOUT_S=1800`。開 `dev`→`main` 的發版 PR。
  - 驗收：部署紀錄 `deploy deployed … ok`；bot 與 webapi 健康、交易狀態不變；包絡仍是 2–2，所以發版後沒有任何 7 天期的單，evidence 的 `tenor` 顯示第 1 條（包絡不允許）不成立。查詢（唯讀）：`select model_evidence->'optimizer'->'tenor' from execution_decisions order by occurred_at_ms desc limit 5`（步驟 2 落地時確認的路徑）。貼出部署紀錄與一筆 evidence。
  - 需要人做的事：owner 核准發版；驗收的唯讀查詢要 owner 核准。
  - 完成後的狀態：規則在線但不動作，等步驟 5。

- [x] **5. 放寬包絡的天期上限**（被擋於：步驟 4；owner 核准）
  - 範圍：在 VM 以 `scripts/amend_capital_policy.py` 把 fUST 的 `max_period_days` 由 2 改成 7：先 dry run 取得報告與 digest，核對報告只改這一欄，再 `--apply-digest` 與 `--reason`（`docs/runbooks/operations.md` §2）。
  - 驗收：`sudo bfx-op status` 顯示 fUST 的包絡是 2–7、其餘欄位不變；貼出 dry run 報告與套用後的狀態。
  - 需要人做的事：owner 核准；操作要用 owner role 的秘密檔。
  - 完成後的狀態：規則開始生效，最多掛 1 筆 7 天期單。

- [ ] **6. 第零步：看第一筆 7 天期借款記的天期**（被擋於：步驟 5）
  - 範圍：等第一筆 7 天期掛單成交，讀 `venue_credit_mirror`（或 `funding_credit_history`）那筆借款的 `period_days`，並記下掛出到成交的時間、成交利率、逾時撤回的次數。
  - 驗收：把那筆借款的列（id、天期、利率、開始時間）與對應的 execution decision evidence 貼進進度表。
  - 停止條件：(a) 借款記的天期不是 7 → 把包絡改回 2–2（owner 核准），停在這裡，交 D3；(b) 步驟 4 設定的截止日到了還沒有任何一筆成交 → 回報可見率與條件成立的時數，交 owner 決定延長或放棄。
  - 需要人做的事：對正式 DB 的唯讀查詢要 owner 核准。

- [x] **7. 持有時間報告依天期分組並補指標**（被擋於：無；可與步驟 1–6 並行，須在步驟 8 之前完成）
  - 檔案：修改 `backend/src/bfx_funding_bot/modules/backtest/hold_times.py`、`backend/scripts/report_credit_hold_times.py`、兩者的測試、`backend/ARCHITECTURE.md`。
  - 範圍：摘要改成依（kind, 天期）分組，每組多輸出「持有時間中位數佔期限的比例」。`loans_that_became_credits` 改成比對兩者的開始時間。新增兩個指標，定義如下（D6），只對 credit 列算：
    - **指標 3**：對每筆借款，沿用還款規則的同一個序列（2 天期每小時收盤）、同一個 margin（0.2，即預登記的 20%）與同一個覆蓋率門檻（期限的 90%），找期限內第一次收盤低於「鎖定利率 × 0.8」的時刻。分母＝實際結束前曾發生這種下跌的借款；分子＝其中「提前結束」（沿用既有定義：距到期超過 1 小時）且結束時間在那個時刻之後 24 小時內的。自然到期的不算進分子。同時輸出分母。分母為 0 時比例輸出 `None`。
    - **指標 4**（7 天期與同期 2 天期的**利率差**）：對每筆已結束的 7 天期 credit i，取它從開始到實際結束的這段時間；對照值＝同幣別、利率大於 0（排除 FRR 標記）的 2 天期 credit 在這段時間內「金額 × 重疊時間」加權的平均利率（一筆 2 天期借款只算它落在這段時間內的部分；7 天期借款結束之後的時間不算）；差 d_i ＝ 7 天期借款的利率 − 對照值（單位：每 USDt 每日）。這是利率差，不含 2 天期資金在兩筆借款之間的閒置，所以對 7 天期略為不利；報告的說明要寫出這一點。沒有任何 2 天期借款與它重疊的 7 天期借款不列入，另外輸出被排除的筆數。整體的差＝以「金額 × 持有時間」加權的 d_i 平均。信賴區間：以列入的 7 天期借款為單位重抽 10,000 次、固定 seed 的 bootstrap 95% 區間（取 2.5 與 97.5 百分位）。樣本門檻以「已結束的 7 天期 credit ≥ 20 筆」計；達到門檻但列入的不足 20 筆時，照樣輸出差與區間，並標示列入筆數；列入的是 0 筆時差與區間輸出 `None`。未達門檻時差與區間輸出 `None`。報告要在最後一筆 7 天期借款結束滿 2 天之後才跑（那時與它重疊的 2 天期借款都已結束、進到資料裡）。
  - 消費端：`rg -n "hold_times|report_credit_hold_times|loans_that_became_credits" backend`。
  - 不能動：還款規則本身（`held_ms_until_refinance`）與 margin 預設。
  - 驗收：單元測試（不同天期不混在一組；中位數比例；指標 3 的分母只含結束前發生過下跌的借款、自然到期的不進分子；指標 4 的加權、沒有重疊的借款被排除並計數、未達門檻時回 `None` 而不是 0、達門檻但有借款被排除時仍輸出並標示列入筆數、利率為 0 的 2 天期借款不進對照、固定 seed 下區間可重現；2026-11-21 那種 loan 先於 credit 結束的形狀被判成轉換）；mutation：指標 3 分母改成全部借款、指標 4 改成不加權、樣本門檻拿掉、轉換判定改回比對結束時間，各自要讓測試失敗。
  - 實作與原文的差異（2026-11-22，`/code-review` 後）：(a) 指標 3 的下跌改在「借款實際借出的那段時間」裡找，覆蓋率門檻（90%）也只要求那一段；原文要求整個期限的覆蓋率，會讓第一天就還款的 7 天期借款在報告跑的時候（K 線還沒到期限終點）被整筆排除，留下的都是持有到期的，偏向「不會提前還款」。還款規則本身與 `position` 的算法沒動。(b) 利率不大於 0（FRR 標記）的 7 天期借款不算進指標 4 的樣本與門檻。(c) 「最後一筆結束滿 2 天後才跑」由程式檢查：未滿時不輸出利率差並標示。(d) 轉換判定除了比開始時間，還要求 loan 不比 credit 晚結束。(e) 「看起來是轉換的 loan」仍是每個幣別一個數字，沒有依天期拆開（審查的 nit，未做）。

- [ ] **8. 發版把上限調到正式值**（被擋於：步驟 6 的結果是記 7 天（D3＝A）、步驟 7；owner 核准）
  - 檔案：修改 `deploy/vm/live.env`。
  - 範圍：`BFX_LONG_TENOR_MAX_AMOUNT=600`、`BFX_LONG_TENOR_MAX_OFFERS_TOTAL=30`、`BFX_LONG_TENOR_MAX_ENDED_CREDITS=20`、`BFX_LONG_TENOR_UNTIL=<本步發版日＋70 天>`；`BFX_LONG_TENOR_SINCE` 維持步驟 4 的值（第零步那一張計入累計與樣本）。把開跑日與截止日寫進進度表與 registry。
  - 驗收：部署紀錄；發版後 evidence 的 `tenor` 顯示新的上限（同步驟 4 的查詢）。
  - 需要人做的事：owner 核准發版；驗收的唯讀查詢要 owner 核准。

- [ ] **9. 累積樣本與判讀**（被擋於：步驟 8）
  - 檔案：修改 `docs/adr/2026-09-27-keep-always-2d-over-rate-threshold-tenor.md`（補 Amendment）。
  - 範圍：規則依樣本上限、掛單總上限或截止日自己停止掛 7 天期。停止新掛之後，已在途的借款會照常結束，所以最終的已結束筆數可能略多於 20。等最後一筆 7 天期借款結束滿 2 天，在 VM 用研究帳號跑步驟 7 的報告，依預登記的判讀表下結論；結果與報告寫進 registry，ADR 2026-09-27 補一段 Amendment（位置＞0.75 且中位持有 ≥ 50% 時引用它的 Revocation Trigger 第一條）。實驗期間若發生自動停機，owner 依 runbook 決定是否提前把包絡改回 2–2；提前停止時照「少於 20 筆只報數字」處理。
  - 驗收：報告檔與執行紀錄存在 repo 外的研究筆記；registry 那一列從 TESTING 改成結果；ADR 的 Amendment 引用報告。
  - 停止條件：已結束的 7 天期借款少於 20 筆就到截止日 → 只報數字，不下結論；要延長就重新登記。
  - 需要人做的事：VM 上讀研究用 DB 帳號的執行要 owner 核准。

- [ ] **10. 收尾**：完成定義逐項核對；把包絡改回 2–2 或依判讀留著（D4；改回時照步驟 3 的回滾順序）；獨立設計審查（diff）；延後項附觸發條件搬回主清單；需要人做的事列給 owner。驗收：需求清單 R1–R8 每項指到測試或現場證據；`rg -n "BFX_LONG_TENOR" deploy backend docs` 的結果與 D4 的決定一致；CI 綠。

## 決定

- **D1 要不要做這個實驗、設計是什麼**（狀態：已決 2026-11-22，Will）：做；設計如附件需求 R1–R7。金錢上的輸贏在目前資金下每年 10 USDt 以內，買的是「長天期值不值得做」的答案。
- **D2 「當下 2 天期利率」、逾時秒數、開關放在包絡、自動停機由 owner 判斷、週報不排除 7 天期借款**（狀態：預設（可推翻），寫在前提段）：都不改變預登記的內容；owner 沒有異議就照預設。
- **D3 第一筆 7 天期借款記的天期不是 7 天時怎麼辦**（狀態：未決；擋住步驟 8、9；需要的事實：步驟 6）
  - A：記的是 7 天 → 照計畫繼續。
  - B：記的是借方要的天期 → 掛 7 天量不到長天期行為。停下來，包絡改回 2–2；改成只對「公開 book 上 7 天期的借方出價（bid）」主動成交（taker）才能鎖到 7 天，這是另一個設計，要重新登記。
- **D4 實驗結束後程式與包絡的去留**（狀態：未決；擋住步驟 10；需要的事實：步驟 9 的判讀）
  - A：判讀為值得做 → 規則留著，進入延後項的正式長天期策略設計。
  - B：其餘 → 包絡改回 2–2，下一個 PR 刪掉 `tenor.py` 與接線（讀取面的步驟 1 可留給盤點 9 的延後項）。
- **D5 上限用什麼單位數**（狀態：已決 2026-11-22，Will：A）。預登記寫的是「同時最多 3 筆 7 天期借款、合計 ≤ 600」與「累計 20 筆已結束的 7 天期借款就停」。實作時「筆」有兩種數法：我們送出的掛單（一張最多 200），或 venue 的借款列（一張掛單可能被幾個借方分批吃成好幾筆借款；同一筆放貸還可能先列 loan 再列 credit）。
  - A（建議）：同時曝險只用金額管（借出中＋掛單中 ≤ 600，另外一次只掛一張長天期單）；停止用兩個條件，先到先停：已結束的 credit 列滿 20 筆（與報告判讀同一個定義），或累計成交過的長天期掛單滿 30 張（保險，避免借款列的數法出意外時停不下來）。預登記的「3 筆」不另外數：單筆上限 200 時金額上限 600 約等於 3 張單，但單筆較小時（近期最小 152.74）可以同時有 3 張以上的掛單成交，一張掛單也可能被分成好幾筆借款列，所以同時的借款列可能超過 3。
  - B：照字面數借款列，同時最多 3 列。一張掛單被分成 3 筆借款時，同時上限就用完，資金只用到 200。
  - 兩案都把第零步的「只放 1 張」綁在掛單數上，不依賴借款記的天期。
  - 結論：A。正式階段 `BFX_LONG_TENOR_MAX_OFFERS_TOTAL=30`、`BFX_LONG_TENOR_MAX_ENDED_CREDITS=20`、`BFX_LONG_TENOR_MAX_AMOUNT=600`。
- **D6 指標 3、4 的操作定義**（狀態：已決 2026-11-22，Will）：照步驟 7 寫的定義。指標 4 比的是利率差、不含 2 天期資金的閒置；樣本門檻以已結束的 7 天期 credit 計；最後一筆結束滿 2 天後才跑報告。定義抄回 registry，開跑後不改。

- **D7 長天期讀取的查詢沒有可用的索引**（狀態：已決 2026-11-22，Will：B，不加索引。依據是當天對正式 DB 的唯讀查詢：`ledger_observation_offer_history` 65 列、`ledger_observation_trade` 60 列、`submission_attempt_journal` 48 列、`venue_credit_mirror` 36 列，最大的表 128 kB，遠低於「數十萬列」的門檻。任何一張表長到數十萬列時重開這題。2026-11-22 1b 審查提出；原本擋住步驟 4）。步驟 1 要求「不掃全部歷史」，前提段又寫「本計畫沒有 migration」。實際上 `ledger_observation_offer_history`（只有 `observation_id` 索引）、`ledger_observation_trade`（PK 是 `observation_id, trade_id`）以 `venue_offer_id` 查、`submission_attempt_journal` 以 `(帳戶, symbol, started_at_ms)` 查、`venue_credit_mirror` 讀已結束的列，都沒有對應的索引，每個 tick（約 90 秒）會全表掃。讀取只在設定了長天期時執行，所以在步驟 4 啟用之前正式環境不受影響；讀取失敗時 `deploy` 照跑、拿到 `None`。
  - A：開一個只加索引的 migration（`venue_offer_id` 兩張、attempt 一張）。加索引是增量變更，舊版程式不受影響，回滾仍是部署上一版 digest；但發版要走 migration 的流程（停 webapi 與 bot、備份）。
  - B：不加索引，接受全表掃。只在表小的時候成立。
  - 建議：先對正式 DB 跑一次唯讀的列數查詢（需 owner 核准），四張表都在數十萬列以內選 B，否則選 A。

- **D8 `period_days` 為 NULL 的借款要不要算進長天期的借出中**（狀態：已決 2026-11-22，Will：A，算進借出中、不算進已結束筆數；純計數與 SQL 讀取都已改。這是防線，不是補洞：`/code-review` 查到 `external/bitfinex/observations.py` 的 `parse_credit_observations` 把天期欄列為必填，venue 沒回天期時整次觀測被拒、那個 tick 不部署，所以現行路徑寫不出 `period_days` 為 NULL 的借款；下面「一筆 7 天期借款不會被算進金額上限」的情境在這次修改之前也不會發生。2026-11-22 步驟 3b `/code-review` 提出；原本擋住步驟 8（金額上限 600 生效）；不擋步驟 4–6：第零步只掛 1 張，由累計張數擋住，不靠金額上限）。步驟 1 的驗收寫「`period_days` 為 NULL 的列不算」；但觀測驗證允許借款沒有天期（`ledger/_internal/observation.py` 只拒絕 ≤ 0），而掛單中的長天期單是「天期不明也算」（fail closed）。兩邊不一致：venue 某次沒回天期時，一筆 7 天期借款不會被算進金額上限。
  - A（建議）：仍在借出中、天期不明的借款算進長天期的借出中（與掛單的算法一致）。代價：任何一筆天期不明的借款（包含 2 天期的）在它結束前都會佔金額上限，可能讓長天期暫時掛不出去。
  - B：維持不算，接受 venue 沒回天期時金額上限可能被超過一筆（上限 600、單筆 200）。
  - 需要的事實：正式 DB 的 `venue_credit_mirror` 有沒有出現過 `period_days` 為 NULL 的列（唯讀查詢，需 owner 核准）；從未出現的話兩案在實務上沒有差別。2026-11-22 查到的現況：36 列裡 `period_days` 為 NULL 的 0 列、`mts_opening` 為 NULL 的 0 列（只代表目前表裡的列，不代表 venue 永遠會回天期）。

拍板順序：D1、D5、D6 已拍；D2 是預設；D3 等步驟 6；D4 等步驟 9；D7 已拍（B）；D8 已拍（A）。

## 進度

狀態值：未開始／進行中／程式完成待驗收／完成。

| 步驟 | 狀態 | 已跑的驗收 | 未跑的驗收與原因 | commit |
|---|---|---|---|---|
| 1a 純計數 | 完成 | `tests/modules/ledger/test_long_tenor_exposure.py` 6 passed；`ruff check`、`mypy src/`；mutation 15 個全殺（天期與啟用時間條件、確定沒成交的四個條件逐一、終態不存在當成沒成交、已成交當成沒成交、金額 NULL 當成沒成交、成交列被忽略、已結束算進借出中、loan 或仍在借出的算進已結束筆數；其中「已成交當成沒成交」第一次存活，補了「已成交但兩個金額欄相等」的案例後殺掉） | `/code-review` 與契約審查隨 1b 一起做；CI 待 PR | `66912535` |
| 1b 資料庫讀取與接線 | 完成 | PR #460 的 CI（`ea8e4303`）：`gate (ci)`、`gate (security)`、`gate (capital-rules)` 通過，含全套單元、integration、e2e smoke。本機：`ruff check`、`mypy src/`、`lint-imports`、quality ratchet 綠；受影響單元測試 2229 passed；integration `test_ledger_long_tenor.py` 5 passed、`test_ledger_deployment_input.py` 4 passed（本機 PG18）；mutation（複本）：讀取漏掉 bound 的 offer id、保留已結案 attempt、不查成交列、採用未被接受觀測的終態列、同時間矛盾時取沒成交那列、executed 當成沒成交、`signal_correlation_id` 沒帶、沒設定也讀、periodic 不傳 `long_tenor`、讀取失敗就跳過部署，全殺；SQL 的天期與啟用時間條件拿掉不會失敗（純函式會再過濾，語意由 1a 的 mutation 守住，SQL 條件只限定讀取範圍）。`/code-review` high：8 項（important 3），修 5、記錄 1（拆 `deploy` 的 baseline noqa 併入步驟 2）、交 Will 1（索引，D7）、已處理 1（bot 接線移到步驟 2）；契約審查：3 項（blocking 1＝bot 接線，以改寫計畫處理；should 2：沒有 outcome 的 attempt 算一張已補登進計畫；索引併入 D7） | 第 2 輪審查沒跑（每項修正都有會失敗的測試）；成交列查詢的「只認被接受的觀測」沒有單獨的 mutation 測試 | |
| 2 天期選擇規則 | 完成 | PR #460 的 CI（`ea8e4303`）：`gate (ci)`、`gate (security)`、`gate (capital-rules)` 通過，含全套單元、integration、e2e smoke。本機：`ruff check`、`mypy src/`、`lint-imports`、quality ratchet 綠；受影響單元測試 1750 passed（execution、apps、architecture、ledger）；integration `test_ledger_long_tenor.py`＋`test_ledger_deployment_input.py` 9 passed（本機 PG18，含新增的「沒利率的受管長天期掛單也算掛單中」）；mutation（整個 repo 的複本、`PYTHONPATH` 指向複本，複本基線 126 passed）24 個全殺：六個條件各拿掉、截止日反向、金額不含這筆、第 6 條改比賣價、第 6 條只比門檻、2 天期定不出價仍放行、曝險 None 當 0、tick 內累加拿掉、evidence 拿掉、長天期 decision 沒套用、掛單中的天期不明不算、provenance 矛盾不算、啟用時間篩選拿掉、guard 退回拿掉、optimizer／quote reprice 開機檢查拿掉、部分設定不報錯。`/code-review` high：7 項（important 3）：修 4（guard 被擋退回原天期、掛單中改從帳本受管掛單算、quote reprice 開機拒絕、部分設定與幣別不符開機失敗）、駁回 2（逾時欄位未用：計畫已規定與步驟 3 同一次發版；金額大於上限永遠不選：正式包絡單筆上限 200＝`BFX_LONG_TENOR_MAX_AMOUNT` 第零步的值）、記錄 1（索引併入 D7）；契約審查：3 項（blocking 0）：駁回 1（UNKNOWN 掛單漏看：未解的 UNKNOWN 在 sizing 前就擋住整個幣別，`reconciler.py` 的 `evaluate_before_sizing`／`_fallback_uncertain`）、修 2（optimizer 開機檢查寫進計畫並核對正式環境是 `book_guarded`；條件 3 原文改寫） | 第 2 輪審查沒跑（每項修正都有會失敗的測試）；`bot.py` 的接線（政策傳進 reconciler 與 `LedgerDeploymentInput`、幣別檢查）沒有單元測試，靠步驟 4 的現場 evidence 驗 | |
| 3 逾時與包絡撤回 | 完成 | PR #460 的 CI（`ea8e4303`）：`gate (ci)`、`gate (security)`、`gate (capital-rules)` 通過，含全套單元、integration、e2e smoke。本機：`ruff check`、`mypy src/`、`lint-imports`、quality ratchet 綠；單元 `tests/modules/execution/deployment` 262 passed；integration `test_ledger_long_tenor.py`＋`test_ledger_deployment_input.py` 10 passed（本機 PG18，含新增的「曝險 SQL 失敗時仍拿得到要撤的掛單」）；mutation（整個 repo 的複本，複本基線 154 passed）15 個全殺：拿掉天期條件、逾時條件、包絡條件、不扣額度、無視額度、預設額度改 0、可撤清單含天期不明、包絡讀不到當成 0、拿掉 since 條件、拋例外的撤單不扣額度、曝險 None 仍放行、逾時不加容差、掛單中不加容差、曝險 None 時不撤、撤回移回逐幣別迴圈。`/code-review` high（範圍是整條分支）：10 項（無 blocking 標記）：修 6（SINCE 之前會掛出不被計數的單、額度被其他幣別的 E1 先用光、曝險讀不到時不撤、拋例外的撤單不扣額度、venue 時鐘容差、runbook 的 `already_terminal`）、記錄 3（步驟 3b）、駁回 1（第 6 條在 TAKER 分支比的是送單價：前提段 D2 預設就是「實際會送出的利率」）；契約審查：NO DEFECTS FOUND，1 個 nit 駁回（quote 模式下 E1 重撤：設了長天期政策時開機就拒絕 quote 模式，`reconciler.py` 的建構檢查） | 第 2 輪審查沒跑（每項修正都有會失敗的 mutation）；runbook 那段的 psql 查詢沒對正式 DB 跑（需 owner 核准，步驟 4 前） | |
| 3b 發版前修補 | 完成 | PR #460 的 CI（`ea8e4303`）：`gate (ci)`、`gate (security)`、`gate (capital-rules)` 通過，含全套單元、integration、e2e smoke。本機：`ruff check`、`mypy src/`、`lint-imports`（31 kept）、quality ratchet（`noqa` 744）綠；受影響測試 1805 passed（execution、ledger、apps、architecture 單元加 integration `test_ledger_long_tenor.py`、`test_ledger_deployment_input.py`，本機 PG18；審查修正後重跑 deployment＋長天期 279 passed）；新測試修補前先紅（(a) 2 個、(b) 單元與 integration 各 1 個、`guard_error` 1 個）；mutation（複本，一次一個）3 個全殺：試算改回會發事件的 `evaluate`、SQL 拿掉 `IS NULL`、計數改回要求有 `mts_opening`；(c) 以「`deploy` 沒有 noqa 時 ruff 綠、ratchet 鎖在 744」為證。`/code-review` high（`082accc2..HEAD`）：8 項（blocking 0）：修 3（試算時 guard 逾時或例外要分得出來、三處 docstring 與 mutation 清單、`ARCHITECTURE.md` 未 commit）、交 Will 1（D8）、駁回 4（皆 nit：試算不短路、已結束筆數不含無日期的列、沒有 `dry_evaluate` 的 chain、搬移的 `BLE001`）；契約審查：NO DEFECTS FOUND（逐段對照拆開前後的 `deploy`，無行為差異） | 第 2 輪審查沒跑（修正有會失敗的測試）；CI 第一輪紅在 `capital_mutation_gate` 的 M18 錨點沒跟著拆分改（`ea8e4303` 修正，M17、M18 本機與 CI 都殺掉）；`Mutation report (changed functions)` 第一輪跑滿 30 分鐘被取消（整個 `reconciler.py` 都算改到），它不在 gate 的必要檢查裡；(c) 的「noqa 加回去 ratchet 會紅」沒實跑，依 ratchet「數量必須等於鎖定值」的規則推論 | `c1b4613c`、`9fa5053a`、`e4fba098`、`ea8e4303` |
| 4 發版（第零步設定） | 完成（已發版 2026-11-22；`tenor` evidence 2026-11-24 驗到） | 設定 PR #461（`77d746e2`）與發版 PR #462（merge commit `0b99b806`，Will 核准）CI 綠：`gate (ci)`、`gate (security)`、`gate (capital-rules)`。部署紀錄（VM，唯讀查詢）：`534|deployed|0b99b8062615|migrations_applied=f|2026-11-22 14:03:54+00|service_degraded=f`。bot：`running`、重啟 0 次、`/healthz` 200、容器內 `BFX_LONG_TENOR_*` 9 個、開機後 16 分鐘內沒有 error 或 `boot_refused` log，觀測照常打 venue。webapi：`healthy`。交易狀態不變：`ACTIVE`（id 16，2026-11-21 的 resume）。包絡仍是 fUST 2–2、單筆上限 200（`bfx-op status`）。7 天期掛單 0 張。設定檔在本機以 `long_tenor_policy_from_env` 解析通過 2026-11-24 補驗（VM，唯讀連線 `default_transaction_read_only=on`）：最新 5 筆 execution decision（2026-11-23 21:31Z 至 2026-11-24 02:10Z，都在發版之後）的 `tenor` 都是 `{"why": "envelope", "selected_days": null, "failed_condition": 1, "envelope_max_period_days": 2}` | `tenor` evidence 已於 2026-11-24 驗到（見左欄）。發版當下的紀錄：發版後沒有新的 execution decision（最後一筆是 2026-11-21 21:02Z，資金全數借出中：借款 5 筆、掛單 0 張），要等有資金回來、產生下一筆決策才驗得到，查詢是 `select model_evidence->'optimizer'->'tenor' from execution_decisions order by occurred_at_ms desc limit 5`（表沒有 `created_at` 欄，步驟原文的寫法要改用 `occurred_at_ms`）。`/readyz` 回 503 `startup_not_ready`：readiness 要等開機後第一筆 READY 決策才翻成 ready（`deployment/eligibility.py` 的 `set_ready`），這次發版沒改到 `readiness.py`、`eligibility.py`、`apps/runtime`（`git diff b205806a..0b99b806` 為空），是資金全借出時重啟的既有行為，不是這次造成的 | `77d746e2`、`0b99b806` |
| 5 放寬包絡 | 完成（2026-11-24，Will 核准） | VM 以已部署的 backend image（`0b99b806`）跑 `scripts.amend_capital_policy` 的一次性 container（`migrate.env`）。dry run：`realm` `prod`、`expected_revision` 5、`amendment_digest` `4689afe4…553f41`，`current_policy` 與 `new_policy` 只差 `envelope.max_period_days` 2→7（`max_offer_amount` 200、`min_period_days` 2、`max_open_offers` 6、`rate_floor_ratio` 0.5、`min_rate_apr` 0.01、`enabled` true、`all_available`、`max_cell_fraction` 0.70、`reserve_amount` 0 不變）。套用：`status` `applied`、`new_revision` 6、`new_revision_id` `27410934-89a0-4f09-826a-7fc58c38cbe6`、`resumed` false。套用後 `sudo bfx-op status`：fUST `revision` 6、包絡 `min_period_days` 2／`max_period_days` 7、其餘欄位同上；交易狀態 `ACTIVE`；bot `running`、重啟 0 次 | 套用後還沒有新的 execution decision（最後一筆 2026-11-24 02:10Z，早於套用），所以還沒看到 `tenor` 讀到 `envelope_max_period_days: 7`；下一筆決策產生時在步驟 6 一起看 | — |
| 6 第零步判讀 | 未開始 | | | |
| 7 報告依天期分組 | 完成 | PR #464 的 CI（`a14b8f3e`）：`gate (ci)`、`gate (security)`、`gate (capital-rules)` 通過。本機：`ruff check`、`mypy src/`、`lint-imports`、quality ratchet 綠（沒有新增 suppression，重抽用 numpy 的產生器）；`tests/modules/backtest`、`tests/scripts/test_report_credit_hold_times.py`、`tests/architecture` 477 passed。mutation（複本，一次一個）13 個全殺：指標 3 分母改成全部借款、對照利率不加權、樣本門檻拿掉、轉換判定改回比對結束時間（以上是計畫指定的四個）、自然到期算進分子、FRR 借款進對照、拿掉 24 小時窗、整體的差不加權、不同天期混在一組、下跌判定要求整個期限的覆蓋率、FRR 的長天期借款被計分、跑得太早仍輸出、loan 比 credit 晚結束仍算轉換。`/code-review` high：7 項（blocking 0）：修 6（見步驟 7 的「實作與原文的差異」a–d、`--long-period-days` 不得小於等於 2、bootstrap 區間兩側各留同樣數量）、駁回 1（nit：轉換數沒有依天期拆開） | 第 2 輪審查沒跑（每項修正都有會失敗的 mutation）；報告還沒對正式資料跑過（要等有已結束的 7 天期借款，步驟 9） | `af2adbad`、`a14b8f3e` |
| 8 發版（正式上限） | 未開始 | | | |
| 9 累積樣本與判讀 | 未開始 | | | |
| 10 收尾 | 未開始 | | | |
