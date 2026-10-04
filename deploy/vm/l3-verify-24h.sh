#!/bin/bash
# 24h L3 stability check: divergence must stay off across a full day of ticks.
#
# v2 (2026-07-27). Two fixes, both found by checking whether this would have
# produced anything at all:
#   1. OUT used ~ while the systemd unit runs as root, so every write went to
#      /root/bfx/reports/ and failed. The 24h check would have produced NOTHING
#      tomorrow and we would have waited 11 hours to discover that. Same defect
#      as halt-watch.sh had. Absolute paths now.
#   2. It reported BFX_ALLOCATION_CAP_USDT as the halt indicator. That knob was
#      proven inert today (it only binds symbols missing from the safety config,
#      and none are), and the halt now lives in the trading_halt table. It asks
#      the daemon what it WOULD do instead.
REPORTS=/home/ubuntu/bfx/reports
OUT="$REPORTS/l3-verify-24h-$(date -u +%Y%m%d-%H%M).txt"
{
  echo "===== L3 24h 穩定性驗收 @ $(date -u '+%Y-%m-%d %H:%M UTC') ====="
  echo "sha=$(docker exec bfx-bot printenv BFX_SERVICE_VERSION 2>/dev/null </dev/null)"
  n_div=$(docker logs --since 24h bfx-bot 2>&1 | grep -c "divergence_detected")
  n_tick=$(docker logs --since 24h bfx-bot 2>&1 | grep -c "scheduler_tick")
  echo "divergence=$n_div  tick=$n_tick"
  if [ "$n_tick" -gt 0 ]; then
    echo "觸發率=$(awk "BEGIN{printf \"%.1f%%\", $n_div*100/$n_tick}")"
  fi
  echo "--- LOCF 補格（raw < filled 表示仍有讀不到的 candle）---"
  docker logs --since 24h bfx-bot 2>&1 | grep "scheduler_tick" \
    | sed -E "s/.*raw=([0-9]+) filled=([0-9]+)/\1 \2/" | sort | uniq -c | sort -rn | head -5
  echo "--- 若仍有 divergence，樣本 ---"
  docker logs --since 24h bfx-bot 2>&1 | grep "divergence_detected" | tail -2 \
    | sed -E "s/.*cell=([^ ]+).*live=ExtractedSignal\(signal_score=([-0-9.e]+).*replay=ExtractedSignal\(signal_score=([-0-9.e]+).*/\1 live=\2 replay=\3/"
  echo "--- 已收盤 bar 被改寫的證據（revocation trigger）---"
  docker exec --user postgres bfx-postgres psql -U bfx -d bfx -tAc "SELECT count(*) FROM funding_candle_revisions" </dev/null
  echo "--- 封存進度（已定稿 / 未定稿）---"
  docker exec --user postgres bfx-postgres psql -U bfx -d bfx -tAc "SELECT count(*) FROM funding_candles WHERE finalized_at_ms IS NOT NULL" </dev/null
  docker exec --user postgres bfx-postgres psql -U bfx -d bfx -tAc "SELECT count(*) FROM funding_candles WHERE is_final = false" </dev/null
  echo "--- 真 ERROR（用 log level 欄位，不是 grep -i error）---"
  docker logs --since 24h bfx-bot 2>&1 | grep -cE "^[0-9-]+ [0-9:,]+ (ERROR|CRITICAL) "
  echo "--- 交易狀態：問 guard 會不會下單，不是讀 env ---"
  docker exec bfx-bot python -c "
import json, os, urllib.request
t = os.environ['BFX_ADMIN_TOKEN']
def call(p, m='GET', d=None):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        'http://127.0.0.1:8080'+p, data=d,
        headers={'Authorization': 'Bearer '+t}, method=m), timeout=15))
s = call('/admin/trading-status')
p = call('/admin/dry-evaluate', 'POST', b'')
print('halted:', s['halt']['halted'], '|', s['halt']['reason'])
print('sources:', json.dumps(s['halt']['sources']))
print('would_submit_any:', p['would_submit_any'])
for sym, r in sorted(p['symbols'].items()):
    print('  ', sym, 'blocked_by=', r['blocked_by'],
          'spendable=', s['symbols'].get(sym, {}).get('spendable'))
print('last_submit_attempt:', json.dumps(s['last_submit_attempt']))
" </dev/null 2>&1
  echo "DONE"
} > "$OUT" 2>&1
ln -sf "$OUT" "$REPORTS/l3-verify-24h-latest.txt"
