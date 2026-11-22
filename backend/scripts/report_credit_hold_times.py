"""How long borrowers held our funding credits, against the replay's repayment scenarios.

Read-only. Loads our ended credits (`funding_credit_history`, the venue's own
record of each credit's rate, term, opening and actual close) and the hourly
2-day market candles, then reports per symbol, credit kind and term: how many closed
early, the amount-and-term weighted share of the term actually held, what the
refinance-on-drop rule (ADR 2026-09-27 D3) would have predicted for the same
credits, and where the measurement sits between that rule (0) and held to
maturity (1). Venue credits (in use) and venue loans (taken, unused) are
reported separately, never added together: one lend may be listed as both.
Terms are never pooled either. For venue credits it adds, per term, how many
were repaid within a day of the market falling under the rule's trigger, and
for the long term (`--long-period-days`) its rate against the 2-day credits
lent over the same hours. Run it at least two days after the last long credit
ended: by then the 2-day credits that overlapped it have ended and are in the data.

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
from uuid import UUID

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.hold_times import (
    BASE_PERIOD_DAYS,
    DropFollow,
    EndedCredit,
    HoldRow,
    HoldSummary,
    RateGap,
    group_holds,
    loans_that_became_credits,
    measure_holds,
    rate_gap,
    repaid_after_drop,
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
DEFAULT_LONG_PERIOD_DAYS = 7

CAVEATS = (
    "Small sample from one account and one currency. Each term is its own row: a term with "
    "no row here was not measured, and one term says nothing direct about another.",
    "Venue credits and venue loans are never added together: one lend may be listed first as "
    "a loan and then as a credit. 'loans that look converted' counts loans that opened within a "
    "minute of a same-sized, same-rate, same-term credit (a heuristic); such a loan's "
    "end is not a repayment, so the loan row overstates early closes by about that many.",
    "'repaid after a drop' is over venue credits the market fell under the rule's trigger for "
    "before they ended: the share repaid early within 24 hours of that close. A credit that "
    "then ran to its term counts against it. The drop is looked for over the hours the credit "
    "was actually out; a credit whose hours the candles do not cover is left out.",
    "Only ended credits are read. A credit still out is in no row, so a report run while "
    "long credits are out sees only the ones repaid early and overstates early repayment "
    "for that term.",
    "The rate gap is the long credit's rate minus the amount-and-overlap weighted rate of the "
    "2-day credits (fixed rate only) lent during its life, per unit per day. It is a rate "
    "difference: it leaves out the hours 2-day money sits idle between credits, so it is "
    "slightly unfavourable to the long term. It is reported from 20 ended long credits on; "
    "long credits no 2-day credit overlapped are left out and counted. The interval is a "
    "10,000-resample bootstrap over the included long credits, fixed seed.",
    "Credits with a rate of 0 or below (the venue's FRR marker) and credits whose term runs "
    "past the loaded candles without the rule firing are left out of the comparison.",
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
    account_id: UUID | None = None  # the one account the credits belong to


def single_account(account_ids: Sequence[UUID], scope: str) -> UUID | None:
    """One report describes one account's borrowers: refuse a pooled sample."""
    distinct = sorted(set(account_ids), key=str)
    if len(distinct) > 1:
        raise SystemExit(f"{scope}: credits from {len(distinct)} exchange accounts; pass "
                         "--exchange-account-id")
    return distinct[0] if distinct else None


def credits_from_rows(
    rows: Sequence[FundingCreditHistoryRow], symbol: str
) -> tuple[list[EndedCredit], int, UUID | None]:
    """Ended credits of one account, the rows left out for having no close, the account."""
    account_id = single_account([row.exchange_account_id for row in rows], symbol)
    credits = [
        EndedCredit(credit_id=row.credit_id, kind=row.kind, symbol=row.symbol, rate=row.rate,
                    period_days=row.period_days, amount=abs(row.amount),
                    opened_mts=row.mts_opening, closed_mts=row.mts_last_payout)
        for row in rows if row.mts_last_payout is not None
    ]
    return credits, len(rows) - len(credits), account_id


def reference_end_mts(credits: Sequence[EndedCredit]) -> int:
    """The rule needs candles to each credit's term end, not only to its actual close."""
    return max(max(c.closed_mts, c.opened_mts + c.period_days * 24 * _MS_PER_HOUR)
               for c in credits) + _REFERENCE_PAD_MS


async def _load(symbols: Sequence[str], account_id: UUID | None) -> list[SymbolData]:
    engine = make_engine(Settings())
    session_factory = make_session_factory(engine)
    out = []
    try:
        for symbol in symbols:
            async with session_scope(session_factory) as session:
                query = select(FundingCreditHistoryRow).where(
                    FundingCreditHistoryRow.symbol == symbol)
                if account_id is not None:
                    query = query.where(FundingCreditHistoryRow.exchange_account_id == account_id)
                rows = (await session.execute(query.order_by(
                    FundingCreditHistoryRow.mts_opening, FundingCreditHistoryRow.credit_id,
                ))).scalars().all()
                credits, open_rows, found_account = credits_from_rows(rows, symbol)
                reference: list[FundingCandle] = []
                if credits:
                    reference = await get_candles_in_range(
                        session, symbol=symbol, timeframe="1h", period_agg="p2",
                        start_mts=min(c.opened_mts for c in credits) - _REFERENCE_PAD_MS,
                        end_mts=reference_end_mts(credits))
            out.append(SymbolData(symbol, credits, open_rows, reference, found_account))
    finally:
        await engine.dispose()
    return out


def _f(value: Decimal | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _hours(ms: int | None) -> str | None:
    return None if ms is None else str((Decimal(ms) / _MS_PER_HOUR).quantize(Decimal("0.0001")))


@dataclass(frozen=True)
class SymbolReport:
    data: SymbolData
    rows: list[HoldRow]
    summaries: dict[tuple[str, int], HoldSummary]
    drop_follow: dict[int, DropFollow]  # venue credits, by term
    gap: RateGap


def build_report(
    data: SymbolData, margin: Decimal, long_period_days: int, as_of_mts: int,
) -> SymbolReport:
    rows = measure_holds(data.credits, data.reference, margin)
    groups = group_holds(rows)
    return SymbolReport(
        data=data, rows=rows,
        summaries={key: summarize_holds(group, margin) for key, group in groups.items()},
        drop_follow={period: repaid_after_drop(group)
                     for (kind, period), group in groups.items() if kind == "credit"},
        gap=rate_gap(rows, period_days=long_period_days, as_of_mts=as_of_mts),
    )


def _gap_line(gap: RateGap) -> str:
    counts = (f"{gap.n_long} ended {gap.period_days}-day credits, {gap.n_included} included, "
              f"{gap.n_excluded} with no overlapping 2-day credit")
    if not gap.settled:
        return (f"Rate gap, {gap.period_days}-day against 2-day: not reported, run too early "
                f"({counts}; the newest {gap.period_days}-day credit ended less than two days "
                "ago, so the 2-day credits lent alongside it may still be out).")
    if gap.gap is None:
        return (f"Rate gap, {gap.period_days}-day against 2-day: not reported ({counts}; "
                f"reported from {gap.min_sample} ended).")
    return (f"Rate gap, {gap.period_days}-day against 2-day: {_f(gap.gap, 8)} per day "
            f"(95% interval {_f(gap.ci_low, 8)} to {_f(gap.ci_high, 8)}; {counts}).")


def render_markdown(reports: Sequence[SymbolReport], *, margin: Decimal) -> str:
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
    for report in reports:
        data = report.data
        lines += [
            "",
            f"## {data.symbol} — {len(data.credits)} ended credits "
            f"({data.open_rows} rows without a close time left out)",
            "",
            f"Loans that look converted into a credit: {loans_that_became_credits(data.credits)}.",
            "",
            "| kind | term d | credits | closed early | median held h | median held share "
            "| held share of term | compared "
            "| held share (compared) | refinance rule share | position | early with market print "
            "| of those, market more than the margin below rate | saw a drop | repaid after a drop |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for (kind, period), s in report.summaries.items():
            follow = report.drop_follow.get(period) if kind == "credit" else None
            lines.append(
                f"| {kind} | {period} | {s.n} | {s.n_early} | {_f(s.median_held_hours, 1)} | "
                f"{_f(s.median_held_share)} | "
                f"{_f(s.held_fraction)} | {s.n_compared} | {_f(s.compared_held_fraction)} | "
                f"{_f(s.predicted_fraction)} | {_f(s.position)} | {s.early_with_market} | "
                f"{s.early_with_drop} | {'-' if follow is None else follow.n_dropped} | "
                f"{'-' if follow is None else f'{follow.n_repaid_after} ({_f(follow.share)})'} |"
            )
        lines += ["", _gap_line(report.gap)]
    return "\n".join(lines) + "\n"


def _plain(values: object) -> dict[str, object]:
    return {key: (str(value) if isinstance(value, Decimal) else value)
            for key, value in vars(values).items()}


def to_json(reports: Sequence[SymbolReport], *, margin: Decimal) -> dict[str, object]:
    def summary(s: HoldSummary) -> dict[str, object]:
        values = _plain(s)
        if s.median_held_hours is not None:
            values["median_held_hours"] = str(s.median_held_hours.quantize(Decimal("0.0001")))
        return values

    return {
        "refinance_margin": str(margin),
        "symbols": [
            {
                "symbol": report.data.symbol,
                "exchange_account_id": str(report.data.account_id) if report.data.account_id else None,
                "open_rows_left_out": report.data.open_rows,
                "loans_that_look_converted": loans_that_became_credits(report.data.credits),
                "summaries": {f"{kind}_{period}d": summary(s)
                              for (kind, period), s in report.summaries.items()},
                "repaid_after_drop": {f"credit_{period}d": _plain(follow)
                                      for period, follow in report.drop_follow.items()},
                "rate_gap": _plain(report.gap),
                "credits": [
                    {"credit_id": row.credit.credit_id, "kind": row.credit.kind,
                     "rate": str(row.credit.rate), "period_days": row.credit.period_days,
                     "amount": str(row.credit.amount), "opened_mts": row.credit.opened_mts,
                     "closed_mts": row.credit.closed_mts, "held_hours": _hours(row.held_ms),
                     "early": row.early,
                     "drop_at_close": (str(row.drop_at_close)
                                       if row.drop_at_close is not None else None),
                     "refinance_rule_hours": _hours(row.predicted_ms)}
                    for row in report.rows
                ],
            }
            for report in reports
        ],
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--registry-entry", required=True, type=registry_entry_arg,
                   help="the registry row this measurement belongs to")
    p.add_argument("--output", required=True, type=report_path_arg,
                   help="markdown path (.json and .run.json siblings auto)")
    p.add_argument("--symbols", default="fUST")
    p.add_argument("--exchange-account-id", type=UUID, default=None,
                   help="required when the table holds credits of more than one account")
    p.add_argument("--refinance-margin", default=DEFAULT_REFINANCE_MARGIN)
    p.add_argument("--long-period-days", type=int, default=DEFAULT_LONG_PERIOD_DAYS,
                   help="the term whose rate is compared against the 2-day credits")
    args = p.parse_args(list(argv))
    try:
        margin_ok = Decimal(0) < Decimal(args.refinance_margin) < Decimal(1)
    except ArithmeticError:
        margin_ok = False
    if args.long_period_days <= BASE_PERIOD_DAYS:
        p.error(f"--long-period-days must be over {BASE_PERIOD_DAYS}")
    if not margin_ok:
        p.error("--refinance-margin must be a fraction strictly between 0 and 1, e.g. 0.2")
    return args


async def _amain(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = RunStart.begin(args.registry_entry, "report_credit_hold_times")
    margin = Decimal(args.refinance_margin)
    symbols = sorted({s.strip() for s in args.symbols.split(",") if s.strip()})
    everything = await _load(symbols, args.exchange_account_id)
    loaded = [data for data in everything if data.credits]
    for data in everything:
        if not data.credits:
            logger.warning("%s: no ended credits, left out of the report", data.symbol)
    if not loaded:
        logger.error("no ended credits for %s", symbols)
        return 2
    # Across symbols too: one report, one account. The record names the account measured.
    account_id = single_account([data.account_id for data in loaded if data.account_id],
                                ",".join(symbols))
    missing = [data.symbol for data in loaded if not data.reference]
    if missing:
        logger.error("no 2-day candles over the credits of %s", missing)
        return 2
    as_of_mts = int(start.started_at.timestamp() * 1000)
    reports = [build_report(data, margin, args.long_period_days, as_of_mts) for data in loaded]
    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(reports, margin=margin))
    out.with_suffix(".json").write_text(
        json.dumps(to_json(reports, margin=margin), indent=2))
    record = write_research_run_record(
        out, start,
        params={"symbols": symbols, "refinance_margin": margin,
                "long_period_days": args.long_period_days,
                "exchange_account_id": str(account_id) if account_id else None},
        # The credit rows themselves are in the .json output, whose hash the record carries.
        inputs=[fingerprint_candles(f"candles:{data.symbol}:p2", data.reference)
                for data in loaded],
    )
    logger.info("wrote %s, %s and %s", out, out.with_suffix(".json"), record)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain(sys.argv[1:])))
