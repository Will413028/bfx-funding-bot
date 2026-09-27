"""G3 — live bot-vs-idle validation of the deployed canary MR cells.

Thin CLI over modules/live_validation/g3.py (load + compute) and g3_report.py
(markdown + JSON). Since 2026-09-27 the active arm is venue credit interest
with actual held time and C is the ledger funding-wallet balance; earlier
reports are not comparable (see the report's "Methodology change" section).

A FLAGged ledger reconciliation week inside the gate (the most recent 8
settled weeks) makes the verdict UNRELIABLE. After investigating, an operator
may acknowledge it with ``--ack-week 2026-09-21="reason"`` (repeatable); the
acknowledgement and its reason are echoed in the report. For the weekly
timer run, add the flag to the weekly-report command in docker-compose.bot.yml
so the acknowledgement is recorded in git.

Run from backend/:
  uv run python -m scripts.run_g3_live_validation --out docs/research/<date>-g3-live-validation.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.live_validation.g3_report import render_markdown, verdict_to_json
from bfx_funding_bot.modules.live_validation.weekly_attribution import FEE_RATE


def _parse_capital(raw: str | None) -> Decimal | None:
    """--capital: an explicit C overrides the ledger-derived one.

    There is no environment default any more: BFX_ALLOCATION_CAP_USDT is 0 in
    production since the capital policy replaced allocation caps (2026-09-27
    weekly run died on "capital must be positive"). Without --capital, C per
    window is the funding-wallet balance from funding_interest_payments."""
    if raw is None:
        return None
    capital = Decimal(raw)
    if capital <= 0:
        raise SystemExit(f"--capital must be positive, got {raw}")
    return capital


def _parse_acks(raw: list[str] | None) -> dict[int, str]:
    """--ack-week YYYY-MM-DD=reason → {week start ms: reason}. The date must be
    the Monday a reconciliation week starts on, and the reason is mandatory."""
    acks: dict[int, str] = {}
    for item in raw or []:
        day, sep, reason = item.partition("=")
        reason = reason.strip()
        if not sep or not reason:
            raise SystemExit(f"--ack-week needs YYYY-MM-DD=reason, got {item!r}")
        try:
            start = datetime.strptime(day.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as exc:
            raise SystemExit(f"--ack-week date must be YYYY-MM-DD, got {day!r}") from exc
        if start.weekday() != 0:
            raise SystemExit(f"--ack-week {day} is not a Monday (reconciliation week start)")
        acks[int(start.timestamp() * 1000)] = reason
    return acks


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output .md path")
    parser.add_argument(
        "--capital",
        default=None,
        help="capital budget C (default: per-window funding-wallet balance from the venue ledger)",
    )
    parser.add_argument(
        "--ack-week", action="append", metavar="YYYY-MM-DD=REASON",
        help="acknowledge a FLAGged reconciliation week inside the gate (repeatable)",
    )
    args = parser.parse_args()

    from bfx_funding_bot.modules.live_validation.g3 import build_g3_report

    report = await build_g3_report(capital=_parse_capital(args.capital),
                                   acks=_parse_acks(args.ack_week))
    out = Path(args.out)
    out.write_text(render_markdown(report=report, fee_rate=FEE_RATE))
    out.with_suffix(".json").write_text(
        json.dumps(verdict_to_json(report, fee_rate=FEE_RATE), indent=2)
    )
    print(f"wrote {out} and {out.with_suffix('.json')}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
