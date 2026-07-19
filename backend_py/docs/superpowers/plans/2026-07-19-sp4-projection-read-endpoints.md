# SP4 — 投影讀端點(operator console 核心三端點)

**Date**: 2026-07-19。Will 拍板範圍:核心三端點(不含 ops-summary,等 FE 需要再加)。

## 設計(全沿用既有 pattern)

- 新 router `modules/api/projections.py`,`build_projections_router()`,prefix `/api/v1`。
- **Realm**:`BFX_ACCOUNT_ID`/`BFX_DEPLOYMENT_ENV` env(同 attribution router)——operator
  console v1 讀 bot 單帳戶;SP6 多租戶時再改 user→account 映射。
- **Auth**:每 route `require_user`(同 SP3);`{"data": ...}` envelope、camelCase alias。
- 端點:
  1. `GET /positions` — position_state 全 symbol rows(symbol/reserved/realized/nCredits/
     lastUpdatedMs/lastReconciledAt/lastEventSeq),symbol 排序。
  2. `GET /offers` — offer_claims,預設只回活動中(state ∈ pending/claimed);`?state=` 可指定
     單一 state(含 released/failed 供除錯)。cid/venueOfferId/state/symbol/sizeUsdt/
     occurredAtMs/lastUpdatedMs,last_updated_ms 倒序。
  3. `GET /executions` — event_log 分頁:`?limit=`(default 50,cap 200)+ `?before=`(event_seq
     cursor);eventSeq/eventType/occurredAtMs/symbol/venueOfferId/cid/amount/rate(amount/rate
     從 payload 抽,無則 null),event_seq 倒序。
- 唯讀;不動 daemon/投影寫入方。INERT 對 bot(webapi 只讀共用表)。

## Tasks(TDD)

1. 失敗測試 `tests/test_projections_router.py`(仿 test_attribution_router:sqlite + override
   require_user/get_session;含 realm 隔離、offers 預設過濾、executions cursor、未授權 401)。
2. schemas + router 實作、main.py 註冊。
3. gates(pytest not-integration / mypy / ruff)→ commit → deploy → smoke(`/api/v1/positions`
   無 auth 401、容器內帶 realm 查有資料)。
