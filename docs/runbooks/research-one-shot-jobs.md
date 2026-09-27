# Research one-shot jobs（外部訊號 ingest 與研究腳本）

研究用資料與腳本**不進 live path**：一次性容器、不碰 `bfx-app` 的任何 container、不改
runtime env 檔。結果寫 DB 研究表或 stdout；報告不進本 repo。

## 資料表

| 表 | PK | migration | 寫入腳本 |
|---|---|---|---|
| `perp_funding_rates` | `(venue, symbol, mts)` | `f3b8d2c7a1e4` | `scripts/ingest_perp_funding.py`（Bitfinex deriv status ＋ Binance USD-M funding） |
| `liquidations` | `(venue, pos_id, mts, is_match, is_market_sold)` | `f3b8d2c7a1e4` | `scripts/ingest_liquidations.py`（Bitfinex `/v2/liquidations/hist`，全 symbol 混流） |

兩支腳本都 resume-aware 且冪等：先補 DB max → now 的缺口，再從 DB min 往回走到 API 回空頁；
每 N 頁 commit，中斷後重跑同一指令即可續走。exit code：0 = ok 且 PASS checks 通過、
1 = PASS check 失敗、3 = operational error。

Rate limit：deriv status 客戶端限 18 req/min；liquidations 極嚴（第二個連發 request 就 429，
冷卻數分鐘），預設每 25 s 一個 request、429 後睡 240 s，完整回補要數小時。
Binance `startTime=0` 會被當成未給，需要全量重掃時用 `--binance-start-time 1`（冪等 upsert，安全）。

## 本機（開發 DB）

```bash
cd backend   # 讀 .env 的 DATABASE_URL
uv run python -m scripts.ingest_perp_funding                       # 兩個 venue 全補
nohup uv run python -m scripts.ingest_liquidations > /tmp/ingest_liq.log 2>&1 &
```

## VM（production DB，一次性容器）

以 root 執行（runtime secret 檔是 root 0600）。image 用正在跑的 bot 的 digest（和 production
同一份程式碼，不 build、不 pull）；DB 帳號用 bot 的 restricted role，從 secret 檔讀取，不印出、
不寫進其他檔案：

```bash
IMAGE=$(docker inspect bfx-bot --format '{{.Config.Image}}')   # <repository>@sha256:<digest>

DATABASE_URL=$(sed -n 's/^DATABASE_URL=//p' /opt/bfx/runtime/bot.env) \
docker run -d --name bfx-research-ingest-perp --label autoheal=false \
  --network bfx_default --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges:true --user 1000:1000 -w /app \
  -e DATABASE_URL --entrypoint /app/.venv/bin/python "$IMAGE" \
  -m scripts.ingest_perp_funding
```

liquidations 同一個 pattern，換 `--name bfx-research-ingest-liq` 與 `-m scripts.ingest_liquidations`
（跑數小時）。其他研究腳本（例如 `-m scripts.run_signal_eda ...`）也用同一個 pattern；
要輸出檔案時加一個 uid 1000 可寫的 bind mount，不要 mount 整個 `/app`（會蓋掉 image 內的 venv）。

進度與收尾：

```bash
docker logs bfx-research-ingest-liq 2>&1 | tail -3
docker exec bfx-postgres psql -U <owner> -d bfx -t -c \
  "select count(*), to_timestamp(min(mts)/1000)::date from liquidations"
docker rm bfx-research-ingest-liq     # 結束（exit 0）後清掉
```

## 已知資料特性

- liquidations 沒有 symbol 參數，下游自行過濾；同一 `pos_id` 通常兩筆（initial `is_match=0`
  ＋ matched `is_match=1`）。
- deriv status 快照不是嚴格 1 分鐘網格（偶有重複 mts、偶有缺分鐘）；分析時要 resample/LOCF，
  不可假設 dense grid。
- Binance 只有 8h realized settlement，`markPrice` 可能是空字串。
