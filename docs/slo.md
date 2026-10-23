# SLO

服務目標只量 bot（放貸的那一半）。營運 UI 是唯讀查看，停了不影響放貸，不設目標。

- 視窗：滾動 28 天。Prometheus 在 VM 上、保留 30 天、每 30 秒抓一次 bot 的 `/metrics`；設定在 homelab repo
  `observability/`，不在本 repo。
- 查詢在 VM 上跑：`docker exec prometheus wget -qO- 'http://localhost:9090/api/v1/query' --post-data 'query=<查詢>'`
  （或 Grafana Explore）。
- 現值是第一次量測的結果；目標依現值訂，之後只往嚴格的方向調。

| SLI | 好事件 ÷ 有效事件 | 目標 | 現值（量測日） |
|---|---|---|---|
| bot 存活 | Prometheus 抓得到 bot 的次數 ÷ 抓取次數 | TBD | TBD |
| reconcile 成功率 | `result="ok"` 的 reconcile tick ÷ 全部 tick | TBD | TBD |
| bot 告警送達率 | `outcome="sent"` ÷（`sent`＋`failed`＋`queue_full`） | TBD | TBD |

## 查詢

bot 存活（部署時的計畫停機也算進去，因為那段時間一樣沒有在放貸）：

```promql
avg_over_time(up{job="bfx-bot"}[28d])
```

reconcile 成功率（counter 在重啟時歸零，`increase` 會處理）：

```promql
sum(increase(bfx_reconcile_ticks_total{result="ok"}[28d]))
/ sum(increase(bfx_reconcile_ticks_total[28d]))
```

bot 告警送達率。`deduplicated`、`rate_limited`、`log_only` 是刻意不送，不算有效事件：

```promql
sum(increase(bfx_alerts_total{outcome="sent"}[28d]))
/ sum(increase(bfx_alerts_total{outcome=~"sent|failed|queue_full"}[28d]))
```

範圍：只算 bot 程序送出的告警（`modules/observability/alerts.py`）。主機端的告警（部署結果走 `bfx_notify`、
systemd 失敗走 `bfx-alert@`）與 Grafana 告警規則不在這個數字裡，送達與否靠 Telegram 那端看得到。
bot 整個停掉時，存活由 healthchecks.io 的 heartbeat（homelab `observability/healthcheck/`，每 5 分鐘）通知。

## 錯誤預算政策

任一 SLI 在 28 天視窗內低於目標，就是預算用完：在回到目標以前，只部署修補與降低風險的改動，
並寫一份事故紀錄（`docs/incidents/TEMPLATE.md`），找出吃掉預算的原因。

依預算消耗速度發告警（SRE workbook 第 5 章的 multi-window burn rate）還沒做，觸發條件是恢復真錢放貸；
規則加在 homelab 的 Grafana alerting。
