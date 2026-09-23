# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5

Data window: 2026-05-27..2026-05-30 | fills: 4 | weekly windows: 1

## TL;DR
- **Verdict: INSUFFICIENT_DATA**
- Headline active spread (since inception): 0.00947080301204265497076023392%
- Active-spread 95% CI: [0, 0]

### Reasons
- only 1 weekly windows (need >= 8)

## Honesty caveats
- Held-to-term duration assumption (matured credits have no close event).
- Fills attributed with the conservative shorter period when cell identity is absent.
- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.
- Platform/credit tail (Bitfinex/Tether) is uncapturable here — mitigated by the cap.

## Recommendation
- PASS: live alpha confirmed; scale-up is the operator's call.
- INSUFFICIENT_DATA: keep accruing fills; re-run after more weekly windows.
- FAIL: deployed config does not beat passive live — investigate before scaling.
- UNRELIABLE: yield model diverges from venue truth — fix attribution before trusting.