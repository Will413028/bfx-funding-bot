# Checkpoint 1 Result — 2026-05-10

**Status**: PASS

**Evidence**:
- backfill_candles.py exit code: 0
- Candles round-tripped: 24 (fUST 1h p2, last 24h)
- Sample candle: mts=1778396400000 (2026-05-10T07:00:00+00:00), open=0.00009252, close=0.00009473, high=0.00009681, low=0.00008494, volume=110733.82498216
- Precision check: Decimal("1e-15") tolerance held for all 24 candles
- Manual web-UI spot-check: https://www.bitfinex.com/funding/UST (pending — requires human verification)

**Decision**: PASS → continue to Phase 4

**Time spent on Phases 1-3**: ~2 sessions (Day 1 scaffold + Day 2-3 client/repo/service/CLI)
**Time vs P50 estimate (~3 days)**: On track
