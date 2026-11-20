"""Step 1b items 1 and 4: join our own fills to the public trade tape.

Read-only. Inputs (on the VM, never committed):
  own_trades.csv     funding_trades export (bfx_research, BEGIN READ ONLY)
  offer_periods.csv  ledger_observation_offer_history period_days per offer (bfx_operator, READ ONLY)
  <tape dir>/fXXX.csv public trades: id,mts,amount,rate,period
Usage: python3 period_join.py <dir with own/offer csv> <tape dir>
"""

import csv
import sys
from collections import Counter
from pathlib import Path


def main() -> None:
    own_dir, tape_dir = Path(sys.argv[1]), Path(sys.argv[2])
    own = list(csv.DictReader((own_dir / "own_trades.csv").open()))
    offers = {}
    for r in csv.DictReader((own_dir / "offer_periods.csv").open()):
        offers[r["venue_offer_id"]] = sorted(
            int(x) for x in r["offer_periods"].strip("{}").split(",") if x and x != "NULL"
        )
    want = {int(r["trade_id"]): r for r in own}
    found: dict[int, list[str]] = {}
    for sym in sorted({r["symbol"] for r in own}):
        path = tape_dir / f"{sym}.csv"
        if not path.exists():
            continue
        lo = min(int(r["mts_create"]) for r in own if r["symbol"] == sym) - 86_400_000
        with path.open() as f:
            next(f)
            for line in f:
                parts = line.rstrip("\n").split(",")
                if int(parts[1]) < lo:
                    continue
                tid = int(parts[0])
                if tid in want:
                    found[tid] = parts
    rows, verdict = [], Counter()
    for tid, r in want.items():
        op = offers.get(r["offer_id"])
        pub = found.get(tid)
        pub_period = int(pub[4]) if pub else None
        if pub is None:
            v = "not_in_tape"
        elif not op:
            v = "no_offer_period"
        elif len(op) > 1:
            v = "ambiguous_offer_period"
        elif pub_period == op[0]:
            v = "pub_eq_offer"
        elif pub_period < op[0]:
            v = "pub_lt_offer"
        else:
            v = "pub_gt_offer"
        verdict[v] += 1
        rows.append((r["symbol"], tid, r["mts_create"], r["amount"], r["rate"],
                     r["trade_period"], op, pub_period, pub[2] if pub else None, v))
    print("symbol,trade_id,mts,amount,rate,own_trade_period,offer_periods,pub_period,pub_amount,verdict")
    for row in sorted(rows, key=lambda x: int(x[2])):
        print(",".join("|".join(map(str, c)) if isinstance(c, list) else str(c) for c in row))
    print("# total", len(want), "symbols", Counter(r["symbol"] for r in own))
    print("# verdicts", dict(verdict))


if __name__ == "__main__":
    main()
