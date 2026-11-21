"""How long borrowers held our funding credits, against the replay's repayment scenarios.

Read-only. Loads our ended credits (`funding_credit_history`, the venue's own
record of each credit's rate, term, opening and actual close) and the hourly
2-day market candles, then reports per symbol and credit kind: how many closed
early, the amount-and-term weighted share of the term actually held, what the
refinance-on-drop rule (ADR 2026-09-27 D3) would have predicted for the same
credits, and where the measurement sits between that rule (0) and held to
maturity (1).

    python -m scripts.report_credit_hold_times --registry-entry "<registry row>" \
        --output /reports/<date>-credit-hold-times.md

Writes `<output>.json` (one row per credit) and `<output>.run.json` (the run record).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.hold_times import (
    EndedCredit,
    HoldRow,
    HoldSummary,
    measure_holds,
    summarize_holds,
)
from bfx_funding_bot.modules.backtest.run_record import fingerprint_candles
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.live_validation.tables import FundingCreditHistoryRow
from scripts.research_run_record import (
    RunStart,
    registry_entry_arg,
    report_path_arg,
    write_research_run_record,
)

logger = logging.getLogger("report_credit_hold_times")

_MS_PER_HOUR = 3_600_000
_REFERENCE_PAD_MS = 48 * _MS_PER_HOUR
DEFAULT_REFINANCE_MARGIN = "0.2"  # ADR 2026-09-27 D3
KINDS = ("credit", "loan")

CAVEATS = (
    "Small sample from one account, one currency and (so far) 2-day terms only: it says "
    "nothing direct about how borrowers treat 7-, 14- or 30-day credits.",
    "A venue credit and a venue loan are reported as separate rows; whether one lend can "
    "appear as both over its life has not been checked, so read the kinds separately too.",
    "A close within one hour of the term end counts as the term running out.",
    "The refinance rule is run on the same hourly 2-day closes the replay uses; a credit with "
    "no candle over its life is left out of the comparison and counted.",
    "position = (held share - rule's share) / (1 - rule's share): 0 means borrowers behave like "
    "the refinance scenario, 1 like held to maturity. It is undefined when the rule predicts "
    "no early repayment for these credits.",
)


@dataclass(frozen=True)
class SymbolData:
    symbol: str
    credits: list[EndedCredit]
    open_rows: int  # rows without a close time, left out
    reference: list[FundingCandle]


async def _load(symbols: Sequence[str]) -> list[SymbolData]:
    engine = make_engine(Settings())
    session_factory = make_session_factory(engine)
    out = []
    try:
        for symbol in symbols:
            async with session_scope(session_factory) as session:
                rows = (await session.execute(
                    select(FundingCreditHistoryRow)
                    .where(FundingCreditHistoryRow.symbol == symbol)
                    .order_by(FundingCreditHistoryRow.mts_opening, FundingCreditHistoryRow.credit_id)
                )).scalars().all()
                credits = [
                    EndedCredit(credit_id=row.credit_id, kind=row.kind, symbol=row.symbol,
                                rate=row.rate, period_days=row.period_days,
                                amount=abs(row.amount), opened_mts=row.mts_opening,
                                closed_mts=row.mts_last_payout)
                    for row in rows if row.mts_last_payout is not None
                ]
                reference: list[FundingCandle] = []
                if credits:
                    reference = await get_candles_in_range(
                        session, symbol=symbol, timeframe="1h", period_agg="p2",
                        start_mts=min(c.opened_mts for c in credits) - _REFERENCE_PAD_MS,
                        end_mts=max(c.closed_mts for c in credits) + _REFERENCE_PAD_MS)
            out.append(SymbolData(symbol, credits, len(rows) - len(credits), reference))
    finally:
        await engine.dispose()
    return out


def _f(value: Decimal | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _hours(ms: int | None) -> str | None:
    return None if ms is None else str(Decimal(ms) / _MS_PER_HOUR)


def _groups(rows: Sequence[HoldRow]) -> dict[str, list[HoldRow]]:
    groups = {"all": list(rows)}
    for kind in KINDS:
        groups[kind] = [row for row in rows if row.credit.kind == kind]
    return groups


def render_markdown(
    results: Sequence[tuple[SymbolData, dict[str, HoldSummary]]], *, margin: Decimal
) -> str:
    lines = [
        "# Credit hold times",
        "",
        f"**Run date**: {datetime.now(UTC).isoformat()}",
        f"**Refinance rule**: repaid at the first hourly 2-day close more than {margin} below "
        "the locked rate.",
        "",
        "## Caveats",
        "",
        *(f"- {c}" for c in CAVEATS),
    ]
    for data, summaries in results:
        lines += [
            "",
            f"## {data.symbol} — {len(data.credits)} ended credits "
            f"({data.open_rows} rows without a close time left out)",
            "",
            "| kind | credits | closed early | median held h | held share of term | compared "
            "| held share (compared) | refinance rule share | position | early with market print "
            "| of those, market >= margin below rate |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for kind, s in summaries.items():
            lines.append(
                f"| {kind} | {s.n} | {s.n_early} | {_f(s.median_held_hours, 1)} | "
                f"{_f(s.held_fraction)} | {s.n_compared} | {_f(s.compared_held_fraction)} | "
                f"{_f(s.predicted_fraction)} | {_f(s.position)} | {s.early_with_market} | "
                f"{s.early_with_drop} |"
            )
    return "\n".join(lines) + "\n"


def to_json(
    results: Sequence[tuple[SymbolData, dict[str, HoldSummary]]],
    rows_by_symbol: dict[str, list[HoldRow]], *, margin: Decimal,
) -> dict[str, object]:
    def summary(s: HoldSummary) -> dict[str, object]:
        return {key: (str(value) if isinstance(value, Decimal) else value)
                for key, value in vars(s).items()}

    return {
        "refinance_margin": str(margin),
        "symbols": [
            {
                "symbol": data.symbol, "open_rows_left_out": data.open_rows,
                "summaries": {kind: summary(s) for kind, s in summaries.items()},
                "credits": [
                    {"credit_id": row.credit.credit_id, "kind": row.credit.kind,
                     "rate": str(row.credit.rate), "period_days": row.credit.period_days,
                     "amount": str(row.credit.amount), "opened_mts": row.credit.opened_mts,
                     "closed_mts": row.credit.closed_mts, "held_hours": _hours(row.held_ms),
                     "early": row.early,
                     "drop_at_close": (str(row.drop_at_close)
                                       if row.drop_at_close is not None else None),
                     "refinance_rule_hours": _hours(row.predicted_ms)}
                    for row in rows_by_symbol[data.symbol]
                ],
            }
            for data, summaries in results
        ],
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--registry-entry", required=True, type=registry_entry_arg,
                   help="the registry row this measurement belongs to")
    p.add_argument("--output", required=True, type=report_path_arg,
                   help="markdown path (.json and .run.json siblings auto)")
    p.add_argument("--symbols", default="fUST")
    p.add_argument("--refinance-margin", default=DEFAULT_REFINANCE_MARGIN)
    args = p.parse_args(list(argv))
    try:
        margin_ok = Decimal(0) < Decimal(args.refinance_margin) < Decimal(1)
    except ArithmeticError:
        margin_ok = False
    if not margin_ok:
        p.error("--refinance-margin must be a fraction strictly between 0 and 1, e.g. 0.2")
    return args


async def _amain(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = RunStart.begin(args.registry_entry, "report_credit_hold_times")
    margin = Decimal(args.refinance_margin)
    symbols = sorted({s.strip() for s in args.symbols.split(",") if s.strip()})
    loaded = [data for data in await _load(symbols) if data.credits]
    if not loaded:
        logger.error("no ended credits for %s", symbols)
        return 2
    missing = [data.symbol for data in loaded if not data.reference]
    if missing:
        logger.error("no 2-day candles over the credits of %s", missing)
        return 2
    rows_by_symbol = {data.symbol: measure_holds(data.credits, data.reference, margin)
                      for data in loaded}
    results = [
        (data, {kind: summarize_holds(rows, margin)
                for kind, rows in _groups(rows_by_symbol[data.symbol]).items() if rows})
        for data in loaded
    ]
    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(results, margin=margin))
    out.with_suffix(".json").write_text(
        json.dumps(to_json(results, rows_by_symbol, margin=margin), indent=2))
    record = write_research_run_record(
        out, start, params={"symbols": symbols, "refinance_margin": margin},
        # The credit rows themselves are in the .json output, whose hash the record carries.
        inputs=[fingerprint_candles(f"candles:{data.symbol}:p2", data.reference)
                for data in loaded],
    )
    logger.info("wrote %s, %s and %s", out, out.with_suffix(".json"), record)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain(sys.argv[1:])))
