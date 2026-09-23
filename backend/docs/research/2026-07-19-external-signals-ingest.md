# External-Signals Ingest — Coverage Report (2026-07-19)

外部訊號池 ingest pipeline + 歷史 backfill。07-06 profit review §3 點名的礦：
「perp funding、清算瀑布——從未被同標準證偽，走同一條 FDR-IC funnel，零 live 風險」。
**純研究資料，不進 live path**；給未來 `/strategy-research batch` 的 EDA 漏斗當輸入
（假設：perp funding（槓桿需求）/ 清算瀑布 與 margin lending 利率有 lead-lag）。

## 資料源探勘結論（2026-07-19 實測）

| 源 | Endpoint | 歷史深度 | 頻率 | Rate limit（實測） |
|---|---|---|---|---|
| Bitfinex deriv status | `GET /v2/status/deriv/{key}/hist`（免認證） | tBTCF0/tETHF0 皆 **~2019-08-17~24 起**（2019-08-17 空、08-24 有） | ~1 min / snapshot | 連發 6 req OK；client 限 18 req/min |
| Bitfinex liquidations | `GET /v2/liquidations/hist`（免認證，**無 symbol 參數**，全 symbol 混流） | **~2019-09-30 起**（2019-07-01 空） | event-based | **極嚴**：第 2 個立即 req 就 429，冷卻長達數分鐘；client 限 1 req/25s，429 後 sleep 240s |
| Binance USD-M funding | `GET /fapi/v1/fundingRate`（免認證） | BTCUSDT 2019-09-10 起、ETHUSDT 2019-11-27 起 | 8h realized | 寬鬆；1000/頁；**OCI VM IP 可達（HTTP 200，未被擋）** |

### 實證欄位（不可盡信官方 docs，placeholder 很多）

deriv status hist entry（23 元素）：`[0]` MTS、`[2]` DERIV_PRICE、`[3]` SPOT_PRICE、
`[5]` INSURANCE_FUND_BALANCE、`[7]` NEXT_FUNDING_EVT_TS、`[8]` NEXT_FUNDING_ACCRUED、
`[9]` NEXT_FUNDING_STEP、`[11]` CURRENT_FUNDING、`[14]` MARK_PRICE、`[17]` OPEN_INTEREST、
`[21]/[22]` CLAMP_MIN/MAX。

liquidations entry（**多一層 array 包裝** `[["pos", ...]]`，12 元素）：`[1]` POS_ID、`[2]` MTS、
`[4]` SYMBOL、`[5]` AMOUNT（正=long 被清、負=short 被清）、`[6]` BASE_PRICE、`[8]` IS_MATCH、
`[9]` IS_MARKET_SOLD、`[11]` PRICE_ACQUIRED（initial event 為 null）。
同一 pos_id 通常兩筆事件（initial `is_match=0` + matched `is_match=1`）。

Binance：`{symbol, fundingTime, fundingRate, markPrice}`（markPrice 可為空字串）。

## 落地

- 表：`perp_funding_rates`（PK venue,symbol,mts）+ `liquidations`（PK venue,pos_id,mts,is_match,is_market_sold），
  migration `f3b8d2c7a1e4`（chain 在 `d4e8f1a2b6c9` 之後）。
- 模組：`src/bfx_funding_bot/modules/external_signals/`（tables/schemas/repository/service/client）。
  研究用 client 獨立於 live 的 `BitfinexREST`（live 要 fail-fast、backfill 要 429 sleep-retry，語意相反）。
- Scripts：`scripts/ingest_perp_funding.py`、`scripts/ingest_liquidations.py` —
  resume-aware（top-up 補 db_max→now 缺口 + walk-back 從 db_min 續走）、冪等 upsert、
  每 N 頁 commit（中斷少損失）、PASS checks + exit codes。

## Backfill 結果（VM `oci-a1`，one-shot containers，2026-07-19）

### `perp_funding_rates` — **完成**（PASS checks 全過，exit 0）

| venue | symbol | rows | 覆蓋 | max\|rate\| | avg\|rate\| |
|---|---|---|---|---|---|
| bitfinex | tBTCF0:USTF0 | 3,631,949 | 2019-08-22 08:00 → 2026-07-19 16:26 | 0.0025（=clamp） | 8.27e-5 |
| bitfinex | tETHF0:USTF0 | 3,623,375 | 2019-08-28 06:02 → 2026-07-19 16:26 | 0.0025（=clamp） | 7.75e-5 |
| binance-usdm | BTCUSDT | 7,513 | 2019-09-10 → 2026-07-19 | 0.003 | 1.27e-4 |
| binance-usdm | ETHUSDT | 7,279 | 2019-11-27 → 2026-07-19 | 0.00375 | 1.48e-4 |

表大小 1,944 MB（VM disk 64G free，無虞）。rate 數量級 sanity：bitfinex 8h period
avg ~0.008%、極值頂在 ±0.25% clamp；binance avg ~0.013% — 皆合理。

### `liquidations` — **partial / in-progress**（container `bfx-research-ingest-liq` 續跑中）

截至 2026-07-19 16:26 UTC：**96,868 rows**，walk-back 已到 **2025-08-09**（目標 ~2019-09-30，
外推還需 ~5-7h，25s/req rate limit 是硬上限）。amount>0（long 被清）75,501、
amount<0（short）21,367 — 兩側都有 ✓；182 symbols，top：tBTCF0:USTF0 26.9K、
tETHF0:USTF0 14.8K、tBTCUSD 5.1K。表大小 25 MB。

腳本 resume-aware：container 完成時會自帶 PASS checks 退出（exit 0）；若中途死掉，
重跑同一指令即從 db_min 續走，不重抓。

```bash
# 進度監看
ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@oci-a1 \
  "docker logs bfx-research-ingest-liq 2>&1 | tail -3; \
   docker exec bfx-postgres psql -U bfx -d bfx -t -c \
   'select count(*), to_timestamp(min(mts)/1000)::date from liquidations'"
# 完成後清容器
ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@oci-a1 "docker rm bfx-research-ingest-liq"
```

## 踩坑（VM 實跑炸出，都已修）

1. **Binance `startTime=0` 被當 absent** → 回最新 500 筆而非 2019 歷史。空 DB cursor 要從 1 起；
   `--binance-start-time 1` 可強制全量重掃（冪等 upsert 安全）。
2. **page 內重複 key** → Postgres `ON CONFLICT DO UPDATE cannot affect row a second time`
   （CardinalityViolation；liquidations 第一頁就中、deriv 也有重複 mts）。
   upsert 前先 batch 內 last-wins dedupe。sqlite 測試逐列處理不會炸，掩蓋了這個 bug。
3. **partial page ≠ 資料盡頭** → walk-back 只認空頁終止（Binance forward fill 例外，
   partial=追到 now）。
4. **Bitfinex `end` 是秒解析度** → 歷史最底部 API 會重發 boundary row（oldest=end+1）
   而非回空頁。舊 code 當 cursor-stuck 拋錯還 rollback 掉最後 chunk（一度少了最早 23 天）；
   已改成視為 bottom-of-history 正常收尾。
5. liquidations 429 冷卻長達數分鐘 — 觸發一次就虧大，寧可 25s/req 慢慢走。

## 重跑指令

```bash
# 本機（backend_py/，讀 .env 的 DATABASE_URL）
uv run python -m scripts.ingest_perp_funding            # 兩 venue 全補（resume）
uv run python -m scripts.ingest_liquidations             # 慢（25s/req），建議 nohup

# VM one-shot（不碰 live 容器；先 cd ~/bfx-research 同步到目標 commit）
docker run -d --name bfx-research-ingest-perp --label autoheal=false \
  --network bfx_default -w /app --env-file ~/bfx-funding-bot/.env.runtime \
  -v ~/bfx-research/backend_py/src:/app/src:ro \
  -v ~/bfx-research/backend_py/scripts:/app/scripts:ro \
  --entrypoint python bfx-bot:local -m scripts.ingest_perp_funding
# liquidations 同 pattern，-m scripts.ingest_liquidations（跑數小時）
```

## 已知限制

- liquidations 為全 symbol 混流（API 無 symbol 篩選），下游 EDA 自行過濾。
- deriv status 1-min 快照的 `funding_rate`（CURRENT_FUNDING）是當期套用值、
  `next_funding_accrued` 是下期預估累計 — lead-lag 研究兩者都留了。
- Binance 只有 8h realized settlement（無 predictive 序列）；mark_price 常為空。
- deriv 快照偶有同 mts 重複（last-wins 存檔）；亦偶有缺分鐘（非嚴格 1-min 網格，
  EDA resample 時要 LOCF/對齊，不可假設 dense grid）。
- deriv genesis 附近（BTCF0 2019-08-22 06:40–08:00）有零星 snapshot 因秒解析度邊界
  收不進來（<2h、個位數 rows），對研究無實質影響。
- 每次完整 walk-back 結尾多 1 個空頁 request（終止證明），成本可忽略。
- liquidations backfill 於本報告 commit 時仍 in-progress（見上節監看指令）；
  完成後可重跑本節查詢更新數字，或直接以 DB 為準。
