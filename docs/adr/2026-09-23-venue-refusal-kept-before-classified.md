---
title: 交易所 5xx 的拒絕理由先結構化保存，有真實錯誤碼後才決定哪些算確定被拒
date: 2026-09-23
status: active
tags: [bfx-funding-bot, decision, execution, venue, uncertainty, observability]
---

# 交易所 5xx 的拒絕理由先保存，再分類

## Context

`classify_submit_response` 對 `http_status >= 500` **連 body 都不看就判 UNKNOWN**；body 只以 sha256 摘要
留存（`response_digest`，怕上游回顯憑證）。對通用 HTTP client 這很保守、很正確；但 Bitfinex 會把**業務拒絕**
包成 HTTP 500，理由寫在 body 的 `["error", CODE, MESSAGE]`。

於是「交易所明確說它拒絕、理由是 X」與「交易所掛了、不知道單有沒有進去」變成同一個狀態：每次都開
`submit_outcome_unknown`、擋住整個 symbol、要人工 `zero_match` 裁決。2026-09-21／22 兩次 canary 都如此；
事後手上只剩摘要——嘗試反推 1768 個候選 body 無一命中，真實拒因至今未知。

## Options Considered

- **A. 立刻把帶結構化 error 的 5xx 判成 durable rejection**：依文件與經驗猜一份「屬於送單前驗證失敗」的 code
  允許清單；可以馬上免去人工裁決。
- **B. 先保存、再分類（採用）**：解析文件化的 `["error", CODE, MESSAGE]`，把 code 與限長 message 帶在
  `SubmitOutcomeUnknown` 上並在失敗當下寫日誌；分類仍維持 UNKNOWN，等累積真實 code 後再定允許清單。
- **C. 自動裁決 UNKNOWN**：以查詢證據自動判定是否掛上，不區分拒絕與故障。

## Decision

- **D1**：採 B。只有形狀驗證過的 `(int code, str message)` 會被保留，message 限 200 字元，且若含任一憑證
  就整段丟棄——被回顯的憑證既不是 slot 1 的 int，也不會以原樣通過第二道檢查。原始 body 仍只存摘要。
- **D2（延後）**：把「帶結構化 code 且 code 屬於送單前驗證失敗」的 5xx 判為 `SubmitRejected`（不開 uncertainty、
  不要人工）。4xx 路徑已有同型已審查模式可沿用：`ALLOWLISTED_4XX_REJECTION_STATUSES and rejection_reason is not None`。
  允許清單是 opt-in，未知 code 仍為 UNKNOWN，維持 fail-closed。
- **D3（延後，另需 ADR）**：真正的 UNKNOWN（timeout、連線中斷）的自動裁決。

## Rationale

- **選 B 而非 A**：我們**手上沒有任何一個真實的 Bitfinex 拒絕碼**。A 等於把猜測寫進金流路徑，而且猜錯的方向
  很危險：把一個「其實已掛上」的情況判成 rejected，會釋放實際已承諾的資金。代價：在拿到 code 之前，
  每次 5xx 仍要人工裁決一次。
- **選 B 而非 C**：C 把「交易所自己說明了的拒絕」和「真正不明」混在一起自動化，丟掉的是最有價值的那個分辨。
- **為何 D3 的標準解不通**：業界解「單到底進去沒」靠 client idempotency key ＋ 用該 key 權威查詢；Bitfinex 的
  funding offer submit **沒有 `cid` 欄位**（`build_offer_payload` 註解明載），這正是當初退回人工的原因。
  可行替代是「超過最壞傳播時間的靜默窗口內重複查詢，並以 funding wallet 可用餘額作第二個獨立證人」，
  需要單獨裁定。

## Expected Outcome

- 下一次交易所以 5xx 拒單時，日誌帶出 `venue_error_code` / `venue_error_message`，拒因可知。
- 在 D2 落地前，5xx 仍一律 UNKNOWN；不承諾減少人工裁決。

## Followup

- 蒐集到真實 error code 後落地 D2（允許清單＋回歸測試）。
- D3 另寫 ADR（靜默窗口＋餘額證人）。
- 結構化拒因目前只進日誌；D2 落地時一併寫進 uncertainty evidence（需擴 event schema，與允許清單同時考慮）。

## Revocation Triggers

- Bitfinex 公開完整的 funding 錯誤碼語意 → D2 可直接依文件訂清單。
- 出現任一次「帶結構化 error 的 5xx 事後證明單其實已掛上」→ D2 的前提失效，5xx 永遠維持 UNKNOWN。

## Amendment (2026-10-18): D2 落地

**實收資料。** prod Loki（保留 30 天，約 09-18～10-18）只有 09-21／22 兩筆送單 5xx，都在 D1 之前，沒有 code；
兩筆金額不同，digest 卻相同，代表 body 是不帶金額的固定訊息。D1 上線後約 35 次送單，0 次 5xx。等送單時自然
出現拒絕湊不到樣本，所以改成主動探測：用正式 key 送三筆不可能成交的 fUST offer（可用餘額約 0），回應原文是：

| 送出 amount | HTTP | body |
|---|---|---|
| 1 | 500 | `["error",10001,"Invalid offer: not enough UST balance available in deposit wallet"]` |
| 100000 | 500 | 同上 |
| 0 | 500 | `["error",10001,"Invalid offer: Insufficient Margin Wallet tradable balance to take a loan this large."]` |

- **10001 是通用碼**：餘額不足和金額為 0 回的都是 10001，所以允許清單用 `(code, message)` 整組精確比對，不收前綴。
- **venue 先驗餘額，後驗最低額**：`amount=1` 回的是餘額不足。可用餘額為 0 時錄不到最低額訊息；credit 到期後
  bot 約 4 秒內就重新掛單，這段空檔也接不住。兩組新 body 的 digest 都不等於 09-21／22 的 `0d2110…`，
  所以那兩次不是餘額不足（多半是最低額，尚未證實）。

**落地內容。**

- `VENUE_SUBMIT_REFUSALS_5XX` 只收 `(10001, "Invalid offer: not enough UST balance available in deposit wallet")`，
  而且只在 HTTP 500 時成立（502／503 可能是沒看到 venue 決定的 proxy）；其餘一律 UNKNOWN。
- 金額為 0 那條不收：本機 `validate_amount` 擋掉低於最低額的金額，bot 送不出 0，收進來也用不到。
- 帶結構化錯誤的 UNKNOWN 把 code 與 message（憑證遮蔽）寫進 `transport_outcome_journal.evidence`，不需要改
  schema。下一條拒絕訊息（最低額、其他幣別的餘額訊息）會留在 journal，不會隨日誌過期消失；在 journal 裡看到
  原文後，再補進允許清單並加回歸測試。
- 撤銷條件不變：出現任一次「清單內的 5xx 事後證明單其實已掛上」，就移除該條目，5xx 回到一律 UNKNOWN。

## Lessons

### Rules

- **R1**：以 HTTP status 當語意通道，會把交易所自己的解釋丟掉。`Rule: 對有自訂錯誤協定的交易所 API，先依
  應用層回應分類、HTTP status 只作 fallback；無法確定分類時至少把結構化的錯誤碼保存下來，否則事後無從補救。`

## Related

- 實作 commit `c185a13`（`venue_error()`、`SubmitOutcomeUnknown.venue_error_code/_message`）
- 同 commit 的送單金額決策 [2026-09-23-canary-submit-margin-over-venue-floor](2026-09-23-canary-submit-margin-over-venue-floor.md)
- 拒因不明也是 [2026-09-23-operator-renews-spent-canary-epoch](2026-09-23-operator-renews-spent-canary-epoch.md) 保留人工的理由
- 本 ADR 即原始紀錄，無外部來源文件
- Lessons R1 抽取判決：**未抽**——單一案例且尚無真實錯誤碼佐證；留專案層，待第二個交易所整合案例再抽
