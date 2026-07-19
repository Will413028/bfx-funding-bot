# BorrowRate.tw proof page + free CSV（build-only, no distribution）

**Date**: 2026-07-19。原 2026-05-29 spec 從未落地 git（brainstorm 對話草擬未寫檔）；本檔補上，
範圍縮小：**只建頁面 + CSV,不分發**（r/bitfinex / PTT / TG 分發是 Will 之後自行決定的動作）。

## 目的

bfx-funding-bot 產品化驗證需求面（「有沒有人要買」）的公開素材：
1. **Public proof page**：真實 bot-vs-idle 表現 vs passive baseline（percent-only,歷史)
2. **免費 funding 利率 CSV**：fUST/fUSD 歷史利率下載（今日已 backfill 至 2016,10 年）
3. 兩者皆需能被分享（social share 友善)，但本輪只部署不公開連結

## 合規紅線（強制，來自 [[../../../../wiki/projects/bfx-funding-bot/decisions/2026-05-26-bfx-productization-host-saas|05-26 產品化 ADR]] binding constraint ②）

**行銷零保證收益**（最高法院 112台上字第317號紅線：「看實質不看錢有沒有進你帳戶」）：
- 頁面文案**只能是「歷史數據」語氣**，禁止任何「預期報酬」「保證」「穩賺」字樣
- 必須有顯眼 disclaimer：past performance ≠ future results；非投資建議；非保證收益
- 只顯示**百分比**（APR/return %），**絕不顯示絕對金額**（capital 大小、USDT 面額）——
  這同時也是資安考量（不洩漏 Will 個人資金規模）

## 資料來源（唯讀，不新建 write path）

- Bot 表現：`attribution_weekly` 表（今日已有 API 消費模式可參考：`modules/api/attribution.py`）
  —— 週序列 `realized_apr_net_pct` vs `baseline_close_apr_net_pct` / `baseline_frr_util_apr_net_pct`
- 免費 CSV：`funding_candles`（fUST/fUSD，1h，p2 — 今日 backfill 至 2016-07-31）

## 任務（TDD）

### Backend（新 public router，明確不掛 `require_user`）

1. `modules/api/public.py`：`build_public_router()`，prefix `/api/v1/public`，**無 auth dependency**。
   - `GET /proof-summary`：讀 `attribution_weekly`（單一 cell 或跨 cell 聚合，percent-only）→
     `{ weeks: [{weekStartMs, realizedAprNetPct, baselineFrrUtilAprNetPct}], asOf }`。
     **不回傳** capital_days / gross_interest_usdt / net_interest_usdt（絕對金額欄位一律過濾)。
   - `GET /funding-rates.csv?symbol=fUST`：streaming CSV(date, close_apr_pct)，來源
     `funding_candles`，`symbol` 限 fUST|fUSD 白名單（422 拒其他值）。
   - 兩端點皆設 `Cache-Control: public, max-age=3600`（資料週更/日更，允許瀏覽器/CDN cache 減載）。
   - **不掛 SP5 per-user rate limiter**（無 user_id 可 key）；改用簡單 IP-keyed token bucket
     或至少對 CSV 端點的 row 數設上限（10 年 hourly ≈ 87600 rows，可接受一次性 stream，
     不需分頁）。若加 IP-limiter，複用 `TokenBucketLimiter` 概念但 key 換成 client IP。
2. `main.py` 掛新 router。
3. Gates：TDD 測試（含「不洩漏絕對金額欄位」的 explicit assertion）、pytest/mypy/ruff 全過。

### Frontend（`(marketing)` route group，公開頁，無需登入）

1. `src/app/[locale]/(marketing)/proof/page.tsx`：
   - Headline + 一句話定位（歷史數據語氣，non-committal）
   - Chart：bot vs AlwaysFRR(util-adjusted) 週序列（複用今日 attribution-chart pattern，
     但走 public 端點、無需 auth）
   - Report card：近 N 週平均 realized APR、vs baseline spread、追蹤週數
   - CSV download CTA（連到 public CSV 端點)
   - **顯眼 disclaimer block**（past performance disclaimer,非投資建議,連結見合規紅線段）
2. i18n en + zh-TW。
3. 遵循三個 UI skill（premium-dark-theme / financial-typography / bento-layout-motion）。
4. 測試 + `pnpm lint` + `pnpm build` 過。

## Out of scope（本輪不做）

- 分發到 r/bitfinex / PTT / TG（Will 之後自行決定）
- GO/KILL engagement 量測 pipeline（要先有分發才有量測對象）
- email capture / lead magnet 表單（CSV 直接公開下載，不用換 email，降低本輪範圍）
