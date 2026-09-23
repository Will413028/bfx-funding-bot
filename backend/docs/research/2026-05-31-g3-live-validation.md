# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5

Data window: 2026-05-27..2026-05-31 | fills: 4 | weekly windows: 1

## TL;DR
- **Verdict: INSUFFICIENT_DATA**
- Headline bot-vs-idle (absolute return on budget since inception): 0.04318316361968154385964912281%
- bot-vs-idle 95% CI: [0, 0]

### Reasons
- only 1 weekly windows (need >= 8)

## MR timing alpha (secondary diagnostic)
- MR alpha spread (active − AlwaysMarketRate): 0.00548669883958895126705653021%
- MR alpha 95% CI: [0, 0]
- Diagnostic only — does NOT gate the verdict. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.
- idle arm = 0% by construction; the headline is the bot's own realized return on the allocated budget.

## Honesty caveats
- bot-vs-idle PASS asserts the bot beats an idle balance (earns the market rate), NOT that MR timing beats always-lending — see the MR alpha diagnostic.
- Held-to-term duration assumption (matured credits have no close event).
- Fills attributed with the conservative shorter period when cell identity is absent.
- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.
- Platform/credit tail (Bitfinex/Tether) is uncapturable here — mitigated by the cap.

## Recommendation
- PASS: bot reliably beats idle (earns the market rate on the budget); scale-up is the operator's call.
- INSUFFICIENT_DATA: keep accruing fills/windows; re-run after more weekly windows.
- FAIL: bot does not beat idle (negative-rate regime or realized loss) — investigate before scaling.
- UNRELIABLE: yield model diverges from venue truth — fix attribution before trusting.