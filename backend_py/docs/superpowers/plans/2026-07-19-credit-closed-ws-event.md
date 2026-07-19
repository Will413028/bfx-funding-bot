# CreditClosed WS event — attribution release truth (Option B)

**Date**: 2026-07-19
**Trigger**: 首份 G3 報告 UNRELIABLE。根因已證:借款人提前還款(fcc)無事件路徑,
`open_principal_at` 對「歸還後 2 天內再放出」的本金重複計。實證:attributed 3652.07 −
早還重放的 1338.03 = observed 2314.04(分毫不差)。連帶 `fill_duration_days` 把活 40
分鐘的 credit 記滿 2 天利息 → attribution 分子分母皆歪。

**Will 拍板**:Option B — bot 解析 `fcc` 發 domain event(vs A: weekly chain 拉 venue
credits hist)。

## ADR 邊界(2026-05-29 credit-aware-reconcile-v2 不重開)

- reconcile 仍是 realized/reserved 的 **唯一 writer**(snapshot absolute-set)。
- `CREDIT_CLOSED` 是 **audit/attribution-only** 事件:**零 ledger delta、零 claims
  projection、rebuild delta-tail 忽略**。不碰 single-writer 不變式,故不需重開 ADR,
  僅在 ADR 加 Update 註記。
- 已知限制(接受):WS 斷線期間的 close 事件遺失 → G3 對無 close 事件的 fill 沿用
  period 假設(degrade 回現狀,不會更糟);歷史(< 部署日)無 close 事件,anchor
  divergence 隨新資料累積收斂。

## Tasks(每 task TDD:先紅測試再實作)

1. **auth_ws**: `FccEvent`(layout 同 fcn:0=credit_id,1=symbol,3=mts_create,
   4=mts_update(=close time),5=amount,7=status,12=rate,13=period)+ `parse_frame`
   加 `"fcc"` case。
2. **events + serialization + store**: `CreditClosed` dataclass(symbol, credit_id,
   amount, rate, period_days, mts_create, account_id, is_simulated, venue_seq,
   event_seq, occurred_at_ms=close time, recorded_at_ms;無 cid/venue_offer_id)。
   註冊 `"CREDIT_CLOSED"`。store:`_project_position_state` 顯式 skip(不建/不碰
   position row);rebuild tail fold 忽略。測:roundtrip、append 後 position_state
   不變、rebuild byte-identical。
3. **ws_dispatcher**: `FccEvent → [CreditClosed], [], []`(無 RegistryMutation)。
4. **live_attribution 純函式** `apply_credit_closes(fills, closes) -> list[FillRecord]`:
   close 依 credit_id 去重(取最新)→ 依(symbol, amount 完全相等, fill_ts ≤
   mts_create + 5min slack)配對最近 fill → 該 fill `release_ts_ms = close.occurred_at_ms`
   (若已有更早 release 則取 min)。一 close 配一 fill(greedy by 時間距離)。
   regression fixture = 今日 1338 案例:兩筆同額 fill + 一筆 close 首筆 → open
   principal 恰 2314.04。
5. **兩個消費端接線**:`_g3_loaders` 與 `run_weekly_attribution` 都 fetch
   `CREDIT_CLOSED` rows → `apply_credit_closes`(在既有 RESERVATION_RELEASED
   release_map 之後套用)。
6. **gates + deploy**: pytest -m "not integration" 全過、mypy/ruff clean → commit
   → VM deploy(同批 arm `BFX_LADDER_OBSERVE=true`)→ 驗:log 出現 fcc parse、
   下一次 credit close 後 event_log 有 CREDIT_CLOSED。

## Out of scope

- 不回填歷史 close(等自然累積)。
- 不動 OrderFilled.credit_id 的 live 關聯(fcn 相關性留給未來需要時)。
- position_state/reconcile 語意零改動。
