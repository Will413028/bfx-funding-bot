# SP5 — plan-gating 機制 + per-user 限流(SaaS epic v1 收線)

**Date**: 2026-07-19。Will 拍板:**機制先行、v1 全放行** — 限流真實生效(read 120/min、
write 30/min per-user,env 可調),plan gate 只建 dependency 零 deny 規則(定價未定案;
SP6 多租戶時填 deny 表)。

## 設計

- `modules/api/ratelimit.py`:純 token bucket(注入 clock,可測)+ FastAPI dependency
  `enforce_rate_limit`(depends require_user;bucket key = (user_id, read|write),
  method GET/HEAD=read 其餘=write)。超限 429 + `Retry-After`。**in-memory 單進程**
  (webapi 單 instance;多 instance 時換 Redis——docstring 記載限制)。
  env:`BFX_RATE_LIMIT_READ_PER_MIN`(default 120)/`BFX_RATE_LIMIT_WRITE_PER_MIN`(default 30)。
- `modules/api/plans.py`:`require_plan(minimum)` dependency factory —
  `PLAN_TIERS = {"free": 0, "pro": 1, "operator": 2}`;讀 user_profiles.plan
  (無 row 視同 'free',對齊 server_default);不足 → 403 `plan_required`。
  v1 不掛任何路由(機制 + 測試就緒,SP6 接線)。
- 限流接線:五個 router builder(`routers`/`api_keys`/`attribution`/`config`/`projections`)
  加 router-level `dependencies=[Depends(enforce_rate_limit)]`。

## Tasks(TDD)

1. bucket 純函式測試(允許 N/擋 N+1/refill/read-write 分桶/跨 user 隔離)
2. endpoint 整合測試(429 + Retry-After;write 桶獨立)
3. require_plan 測試(free<pro→403、無 row=free、tier 相等通過)
4. 接線 + 全 gates → commit(deploy 與 FE/metrics 批次一起)
