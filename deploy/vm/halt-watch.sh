#!/bin/bash
# Guard against a silent halt failure.
#
# v2 (2026-07-27): asks the daemon what it WOULD do via POST /admin/dry-evaluate
# instead of inferring from an absence of orders. v1 could only ever say
# "unproven" when the wallet was empty — the same blind spot that let the
# BFX_ALLOCATION_CAP_USDT no-op run for hours.
#
# v1 also never ran: LOG used ~ while the systemd unit executes as root, so
# every invocation died on /root/bfx/reports/. Absolute path now.
LOG=/home/ubuntu/bfx/reports/halt-watch.log
TS=$(date -u '+%Y-%m-%d %H:%M UTC')

# Outcome that matters most: did anything actually get reserved recently?
N_INTENT=$(docker exec bfx-postgres psql -U bfx -d bfx -tAc \
  "SELECT count(*) FROM event_log WHERE event_type='RESERVATION_INTENT' AND occurred_at_ms >= (EXTRACT(EPOCH FROM now())*1000)::bigint - 20*60*1000" \
  2>/dev/null </dev/null | tr -d '[:space:]')

MSG=$(docker exec bfx-bot python -c "
import json, os, urllib.request
n_intent = int('${N_INTENT:-0}' or 0)
t = os.environ['BFX_ADMIN_TOKEN']

def call(path, method='GET', data=None):
    req = urllib.request.Request(
        'http://127.0.0.1:8080' + path, data=data,
        headers={'Authorization': 'Bearer ' + t}, method=method,
    )
    return json.load(urllib.request.urlopen(req, timeout=15))

status = call('/admin/trading-status')
probe = call('/admin/dry-evaluate', 'POST', b'')
halted = status['halt']['halted']
installed = status['halt']['guard_installed']
would = probe['would_submit_any']
funded = sorted(
    s for s, v in status['symbols'].items() if float(v['deployable_headroom']) > 0
)
detail = 'funded=' + (','.join(funded) or 'none')

if n_intent > 0 and halted:
    print(f'HALT FAILED - {n_intent} RESERVATION_INTENT in 20m while halted ({detail})')
elif halted and would:
    # The guards do not stop a synthetic offer even though the halt reads as on.
    print(f'HALT INEFFECTIVE - halted=true but dry-run would submit ({detail})')
elif halted and not would:
    blockers = {
        s: r['blocked_by'] for s, r in probe['symbols'].items() if r['blocked_by']
    }
    print(f'halt CONFIRMED - every symbol blocked {blockers} ({detail})')
elif not installed:
    print(f'manual_kill guard NOT INSTALLED - kill switch has no effect ({detail})')
else:
    print(f'trading enabled - would_submit_any={would} ({detail})')
" 2>&1 </dev/null | tail -1)

echo "$TS ${MSG:-probe failed - could not reach /admin endpoints}" >> "$LOG"
