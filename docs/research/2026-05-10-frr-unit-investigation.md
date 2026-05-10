# FRR Unit Investigation

**Date**: 2026-05-10
**Owner**: Will
**Spec**: `docs/superpowers/specs/2026-05-10-phase3a-frr-unit-investigation-design.md`
**Sample row anchor** (per `funding_stats/schemas.py` docstring):

```
mts=1778411100000, frr=1.12e-06, avg_period=94.98, candle.close (1h p2)=0.00014465
```

## 1. Problem statement

Phase 2 抽樣 `frr × 86400 / candle.close = 668.98`，推翻 5/9「FRR=秒利率」推測。
Phase 3a 走 reference-first → empirical-confirmation 流程解單位。

## 2. Stage 1 reference findings

### 2.1 bitfinex-api-py SDK

[FILL: Stage 1 task 1.2]

### 2.2 ccxt python source

[FILL: Stage 1 task 1.3]

### 2.3 Bitfinex Help Center

[FILL: Stage 1 task 1.4]

## 3. Stage 2 empirical results

[FILL: after Stage 2 runs]

## 4. Conversion factor with provenance

[FILL: only if Gate 1 PASS]

## 5. Caveats

[FILL: at note finalization]

## 6. Next: Phase 3b

[FILL: at note finalization]
