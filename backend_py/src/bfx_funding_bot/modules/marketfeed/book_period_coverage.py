"""Book period coverage audit — Gate A0 (2026-09-22 review §3 / ADR 06-04 Amendment).

Pure aggregation over self-recorded `funding_book_snapshots`: for each symbol,
how often the top-25 ask book carries a level at a given period, how deep that
level is, and what premium it trades at over period 2.

Why it matters: execution eligibility (ARCHITECTURE §4 7c) requires an
*exact*-period ask level before a candidate can leave `book_guarded`
unblocked. AdaptivePeriod's p_mid=7 / p_long=14 offers therefore need period 7
and 14 levels to exist on the book most of the time; `exact_share` is the
number that decides whether those tiers can ever post. `at_least_share`
(any ask with period >= p) is reported alongside as context only — it is NOT
what eligibility checks.

Snapshots hold the top-25 P0 levels per side, the same window the live
provider's checksum covers, so a level invisible here is invisible to
eligibility too. No DB access here; the loader lives in
scripts/audit_book_period_coverage.py.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

DEFAULT_PERIODS: tuple[int, ...] = (2, 7, 14, 30, 120)


@dataclass(frozen=True, slots=True)
class BookAskSnapshot:
    """Ask side of one recorded book: (rate, period, amount) per level."""

    symbol: str
    captured_at_ms: int
    asks: tuple[tuple[Decimal, int, Decimal], ...]


@dataclass(frozen=True, slots=True)
class PeriodCoverage:
    period: int
    n_exact: int
    exact_share: Decimal
    n_at_least: int
    at_least_share: Decimal
    depth_median: Decimal | None
    depth_p10: Decimal | None
    best_ask_median: Decimal | None
    premium_over_p2_median: Decimal | None


@dataclass(frozen=True, slots=True)
class SymbolCoverage:
    symbol: str
    n_snapshots: int
    first_ms: int
    last_ms: int
    period_universe: tuple[tuple[int, Decimal], ...]
    coverage: tuple[PeriodCoverage, ...]


def _dec(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def snapshot_from_payload(
    *, symbol: str, captured_at_ms: int, payload: Mapping[str, Any]
) -> BookAskSnapshot:
    """Parse a `funding_book_snapshots.payload` row; bids are ignored.

    Payload shape (BookSnapshotWriter): {"asks": [[rate, period, count, amount], ...], ...}
    with amounts stored positive.
    """
    asks: list[tuple[Decimal, int, Decimal]] = []
    for level in payload.get("asks", []):
        rate, period, _count, amount = level
        asks.append((_dec(rate), int(period), _dec(amount)))
    return BookAskSnapshot(symbol=symbol, captured_at_ms=captured_at_ms, asks=tuple(asks))


def _nearest_rank(sorted_values: Sequence[Decimal], q: float) -> Decimal:
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def _share(n: int, total: int) -> Decimal:
    return Decimal(n) / Decimal(total) if total else Decimal("0")


def _coverage_for_period(
    snapshots: Sequence[BookAskSnapshot], period: int
) -> PeriodCoverage:
    depths: list[Decimal] = []
    best_asks: list[Decimal] = []
    premiums: list[Decimal] = []
    n_at_least = 0
    for snap in snapshots:
        exact = [lv for lv in snap.asks if lv[1] == period]
        if any(lv[1] >= period for lv in snap.asks):
            n_at_least += 1
        if not exact:
            continue
        depths.append(sum((lv[2] for lv in exact), Decimal("0")))
        best = min(lv[0] for lv in exact)
        best_asks.append(best)
        p2 = [lv[0] for lv in snap.asks if lv[1] == 2]
        if p2 and period != 2:
            base = min(p2)
            if base > 0:
                premiums.append(best / base - Decimal("1"))
    n_exact = len(depths)
    total = len(snapshots)
    depths_sorted = sorted(depths)
    return PeriodCoverage(
        period=period,
        n_exact=n_exact,
        exact_share=_share(n_exact, total),
        n_at_least=n_at_least,
        at_least_share=_share(n_at_least, total),
        depth_median=_dec(median(depths_sorted)) if depths_sorted else None,
        depth_p10=_nearest_rank(depths_sorted, 0.10) if depths_sorted else None,
        best_ask_median=_dec(median(best_asks)) if best_asks else None,
        premium_over_p2_median=_dec(median(premiums)) if premiums else None,
    )


def summarize_period_coverage(
    snapshots: Iterable[BookAskSnapshot],
    periods: Sequence[int] = DEFAULT_PERIODS,
) -> list[SymbolCoverage]:
    """Per-symbol coverage; symbols ordered alphabetically, periods as given."""
    by_symbol: dict[str, list[BookAskSnapshot]] = {}
    for snap in snapshots:
        by_symbol.setdefault(snap.symbol, []).append(snap)

    results: list[SymbolCoverage] = []
    for symbol in sorted(by_symbol):
        snaps = sorted(by_symbol[symbol], key=lambda s: s.captured_at_ms)
        total = len(snaps)
        universe_counter: Counter[int] = Counter()
        for snap in snaps:
            for period in {lv[1] for lv in snap.asks}:
                universe_counter[period] += 1
        universe = tuple(
            (period, _share(n, total))
            for period, n in sorted(
                universe_counter.items(), key=lambda kv: (-kv[1], kv[0])
            )
        )
        results.append(
            SymbolCoverage(
                symbol=symbol,
                n_snapshots=total,
                first_ms=snaps[0].captured_at_ms,
                last_ms=snaps[-1].captured_at_ms,
                period_universe=universe,
                coverage=tuple(_coverage_for_period(snaps, p) for p in periods),
            )
        )
    return results


def _fmt_pct(x: Decimal) -> str:
    return f"{x * 100:.1f}%"


def _fmt_opt(x: Decimal | None, fmt: str) -> str:
    return "-" if x is None else format(x, fmt)


def render_markdown(results: Sequence[SymbolCoverage]) -> str:
    """Operator-facing markdown: period universe + per-period coverage per symbol."""
    lines: list[str] = ["# Book period coverage (Gate A0)", ""]
    lines.append(
        "`exact` = snapshots with an ask level at exactly that period (what eligibility "
        "checks); `>=` = any ask at that period or longer (context only). Depth in "
        "symbol units; premium = best_ask(p)/best_ask(2) − 1, median over snapshots "
        "carrying both."
    )
    for r in results:
        lines += [
            "",
            f"## {r.symbol} — {r.n_snapshots} snapshots, "
            f"captured_at_ms {r.first_ms}..{r.last_ms}",
            "",
            "### Period universe (share of snapshots carrying the period)",
            "",
            "| period | share |",
            "|---|---|",
        ]
        lines += [f"| {p} | {_fmt_pct(s)} |" for p, s in r.period_universe]
        lines += [
            "",
            "### Coverage at target periods",
            "",
            "| period | exact n | exact share | >= share | depth median | depth p10 "
            "| best ask median | premium over p2 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for c in r.coverage:
            lines.append(
                f"| {c.period} | {c.n_exact} | {_fmt_pct(c.exact_share)} | "
                f"{_fmt_pct(c.at_least_share)} | {_fmt_opt(c.depth_median, '.0f')} | "
                f"{_fmt_opt(c.depth_p10, '.0f')} | {_fmt_opt(c.best_ask_median, '.8f')} | "
                f"{'-' if c.premium_over_p2_median is None else _fmt_pct(c.premium_over_p2_median)} |"
            )
    return "\n".join(lines) + "\n"
