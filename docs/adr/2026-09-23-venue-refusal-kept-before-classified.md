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
