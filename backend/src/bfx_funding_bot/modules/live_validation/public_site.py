"""The public proof site, rendered as the static files Cloudflare Pages serves.

ADR 2026-08-31 D6: the public proof page and the free funding-rate CSVs are files
generated ahead of time, never a query on the money database from the internet.
`scripts/build_public_site.py` reads the rows inside the weekly chain (research role)
and calls `render_site`; `deploy/vm/ops/bfx_public_site.py` uploads the directory.
Everything here is pure: rows in, bytes out.

Compliance red line (binding -- 2026-05-26 productization ADR, constraint 2): the
public surface never promises returns and never discloses absolute-dollar figures
(capital size, interest amounts) -- a legal constraint (最高法院 112台上字第317號) and a
privacy one. Every value rendered here is a percentage. `net_interest_usdt` and
`capital_days` are read off the rows only as weights inside `aggregate_weeks`; they
never reach a rendered file (tests/modules/live_validation/test_public_site.py).
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Protocol

# Every file the site consists of, by published path. The uploader accepts exactly
# this set (bfx_public_site.SITE_FILES; tests keep the two equal).
CSV_SYMBOLS: Final = ("fUST", "fUSD")
SITE_FILES: Final = (
    "index.html", "en/index.html", "proof-summary.json",
    *(f"funding-rates-{symbol}.csv" for symbol in CSV_SYMBOLS), "_headers",
)
TRAILING_WEEKS: Final = 12
_DAYS_PER_YEAR = Decimal("365")
_HUNDRED = Decimal("100")


class WeeklyAttribution(Protocol):
    """The attribution_weekly columns the proof reads (AttributionWeeklyRow)."""

    week_start_ms: int
    net_interest_usdt: Decimal
    capital_days: Decimal
    baseline_frr_util_apr_net_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class ProofWeek:
    week_start_ms: int
    realized_apr_net_pct: Decimal | None
    baseline_frr_util_apr_net_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class ProofStats:
    avg_realized_apr_pct: Decimal | None
    avg_baseline_apr_pct: Decimal | None
    spread_pct: Decimal | None
    weeks_tracked: int


def dec_str(value: Decimal) -> str:
    """Canonical fixed-point string: `normalize()` alone prints 1E+1 for ten."""
    return format(value.normalize(), "f")


def aggregate_weeks(rows: Iterable[WeeklyAttribution]) -> list[ProofWeek]:
    """Blend every cell into one bot-vs-baseline weekly series, oldest week first.

    realized: capital-days-weighted APR across cells, Σnet_interest / Σcapital_days ×
    365 × 100 (a per-cell average would misweight small and large cells); None when
    the week has no capital-days (no fills). baseline: plain mean of each cell's own
    util-adjusted FRR baseline (cells differ in symbol and utilization, so there is no
    single market baseline to sum); None when no cell has one.
    """
    by_week: dict[int, list[WeeklyAttribution]] = defaultdict(list)
    for row in rows:
        by_week[row.week_start_ms].append(row)
    weeks = []
    for week_start_ms in sorted(by_week):
        week_rows = by_week[week_start_ms]
        net = sum((r.net_interest_usdt for r in week_rows), Decimal(0))
        capital_days = sum((r.capital_days for r in week_rows), Decimal(0))
        baselines = [r.baseline_frr_util_apr_net_pct for r in week_rows
                     if r.baseline_frr_util_apr_net_pct is not None]
        weeks.append(ProofWeek(
            week_start_ms=week_start_ms,
            realized_apr_net_pct=(net / capital_days * _DAYS_PER_YEAR * _HUNDRED
                                  if capital_days > 0 else None),
            baseline_frr_util_apr_net_pct=(sum(baselines, Decimal(0)) / len(baselines)
                                           if baselines else None),
        ))
    return weeks


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def proof_stats(weeks: Sequence[ProofWeek], window: int = TRAILING_WEEKS) -> ProofStats:
    """Report-card numbers over the trailing `window` weeks; weeks_tracked counts all."""
    recent = weeks[-window:]
    realized = _mean([w.realized_apr_net_pct for w in recent if w.realized_apr_net_pct is not None])
    baseline = _mean([w.baseline_frr_util_apr_net_pct for w in recent
                      if w.baseline_frr_util_apr_net_pct is not None])
    spread = realized - baseline if realized is not None and baseline is not None else None
    return ProofStats(realized, baseline, spread, len(weeks))


def funding_rates_csv(rows: Iterable[tuple[int, float]]) -> bytes:
    """Hourly p2 closes as APR percent: `date,close_apr_pct`, oldest first."""
    lines = ["date,close_apr_pct\n"]
    for mts, close in rows:
        date = datetime.fromtimestamp(mts / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{date},{close * 365.0 * 100.0:.6f}\n")
    return "".join(lines).encode()


def proof_summary_json(weeks: Sequence[ProofWeek], *, as_of: datetime, generated_at: datetime) -> bytes:
    def pct(value: Decimal | None) -> str | None:
        return dec_str(value) if value is not None else None

    body = {
        "weeks": [{"week_start_ms": w.week_start_ms,
                   "realized_apr_net_pct": pct(w.realized_apr_net_pct),
                   "baseline_frr_util_apr_net_pct": pct(w.baseline_frr_util_apr_net_pct)}
                  for w in weeks],
        "as_of": as_of.isoformat(),
        # The uploader refuses a build older than a day (bfx_public_site.MAX_BUILD_AGE).
        "generated_at": generated_at.isoformat(),
    }
    return (json.dumps(body, indent=1, sort_keys=True) + "\n").encode()


# Pages applies these per path (developers.cloudflare.com/pages/configuration/headers).
# No script anywhere: the chart is inline SVG, so the policy allows no script at all.
HEADERS: Final = "\n".join([
    "/*",
    "  Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'",
    "  X-Content-Type-Options: nosniff",
    "  Referrer-Policy: no-referrer",
    "  Cache-Control: public, max-age=3600",
    *(line for symbol in CSV_SYMBOLS for line in (
        f"/funding-rates-{symbol}.csv",
        f'  Content-Disposition: attachment; filename="funding-rates-{symbol}.csv"',
    )),
]) + "\n"


TEXT: Final[Mapping[str, Mapping[str, str]]] = {
    "zh-TW": {
        "title": "BorrowRate — Bitfinex 放貸機器人績效",
        "badge": "每週更新",
        "headline": "Bitfinex 放貸機器人真實績效，每週公開",
        "subtitle": "真實放貸帳戶的歷史數據，對比被動 always-on-FRR 基準。以下所有數字皆為百分比，"
                    "不揭露任何美金金額，亦非預估數字。",
        "as_of": "資料截至 {date}",
        "chart_title": "週度已實現 APR：Bot 與被動基準",
        "chart_subtitle": "跨所有追蹤放貸單元、經手續費調整、以資金天數加權的年化報酬",
        "bot_line": "Bot（實得，扣費）",
        "baseline_line": "AlwaysFRR（利用率調整）",
        "empty": "尚無績效資料：每週歸因排程尚未產出報告。",
        "stat_realized": "近 {weeks} 週平均已實現 APR",
        "stat_spread": "對比基準價差",
        "stat_weeks": "追蹤週數",
        "table": "每週數據",
        "col_week": "週起始（UTC）",
        "csv_title": "免費利率歷史資料",
        "csv_body": "fUST／fUSD 的 Bitfinex 每小時放貸利率（2 天期），免費下載，無需註冊。",
        "csv_cta": "下載 CSV",
        "disclaimer_title": "過去績效不代表未來表現",
        "disclaimer_body": "本頁面所有數字皆為歷史資料，並非對未來報酬的預測、承諾或保證，本頁內容亦不構成"
                           "投資建議。Bitfinex 放貸利率波動劇烈，可能歸零；過去表現不保證未來績效。",
        "other_lang": "English",
        "other_href": "en/",
    },
    "en": {
        "title": "BorrowRate — Bitfinex funding-bot performance",
        "badge": "Updated weekly",
        "headline": "Real Bitfinex funding-bot performance, published weekly",
        "subtitle": "Historical results from a real lending account, benchmarked against a passive "
                    "always-on-FRR baseline. Every figure below is a percentage: no dollar amounts, "
                    "no projections.",
        "as_of": "Data as of {date}",
        "chart_title": "Weekly realized APR: bot vs passive baseline",
        "chart_subtitle": "Fee-adjusted, capital-days-weighted APR across all tracked lending cells",
        "bot_line": "Bot (realized, net)",
        "baseline_line": "AlwaysFRR (util-adjusted)",
        "empty": "No performance data yet: the weekly attribution job has not produced a report.",
        "stat_realized": "Avg realized APR (last {weeks} weeks)",
        "stat_spread": "Spread vs baseline",
        "stat_weeks": "Weeks tracked",
        "table": "Weekly figures",
        "col_week": "Week start (UTC)",
        "csv_title": "Free funding-rate history",
        "csv_body": "Hourly Bitfinex fUST/fUSD funding rates (2-day period). Free download, no signup.",
        "csv_cta": "Download CSV",
        "disclaimer_title": "Past performance is not indicative of future results",
        "disclaimer_body": "The figures on this page are historical data only. They are not a projection, "
                           "prediction, promise, or guarantee of future returns, and nothing here is "
                           "investment advice. Bitfinex funding rates are volatile and can fall to zero; "
                           "past results do not guarantee future performance.",
        "other_lang": "中文",
        "other_href": "../",
    },
}

_BOT_COLOR = "#10b981"
_BASELINE_COLOR = "#94a3b8"
_STYLE = """
:root{color-scheme:dark;--bg:#09090b;--fg:#f4f4f5;--muted:#a1a1aa;--line:#27272a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans TC",sans-serif}
main{max-width:56rem;margin:0 auto;padding:3rem 1rem}
header{text-align:center}h1{font-size:2rem;line-height:1.25;margin:.75rem 0}
.badge{display:inline-block;border:1px solid #10b98133;border-radius:999px;padding:.1rem .75rem;
color:#34d399;font-size:.8rem}.muted{color:var(--muted)}.small{font-size:.8rem}
.lang{position:absolute;top:1rem;right:1rem;color:var(--muted)}
section{margin-top:2.5rem}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(12rem,1fr));gap:1rem}
.card,.panel{border:1px solid var(--line);border-radius:.75rem;padding:1rem 1.25rem}
.stat{font-size:1.75rem;font-variant-numeric:tabular-nums;margin:.25rem 0 0}
svg{width:100%;height:auto}.legend span{margin-right:1.25rem}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:.9rem}
th,td{padding:.35rem .5rem;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
a{color:#34d399}a.button{display:inline-block;margin:.5rem .5rem 0 0;padding:.5rem 1rem;
border:1px solid var(--line);border-radius:.5rem;text-decoration:none}
"""


def _fmt_pct(value: Decimal | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _chart(weeks: Sequence[ProofWeek]) -> str:
    """Two polylines on one percent axis; a week without a value breaks its line."""
    width, height, left, right, top, bottom = 720, 260, 48, 12, 12, 28
    values = [v for w in weeks for v in (w.realized_apr_net_pct, w.baseline_frr_util_apr_net_pct)
              if v is not None]
    low = min([Decimal(0), *values])
    high = max([Decimal(1), *values]) * Decimal("1.1")
    span = float(high - low)
    step = (width - left - right) / max(len(weeks) - 1, 1)

    def point(i: int, value: Decimal) -> str:
        y = top + (height - top - bottom) * float(high - value) / span
        return f"{left + i * step:.1f},{y:.1f}"

    def lines(values: Sequence[Decimal | None], color: str) -> list[str]:
        out, run = [], []
        for i, value in enumerate([*values, None]):
            if value is not None:
                run.append(point(i, value))
                continue
            if len(run) == 1:
                x, y = run[0].split(",")
                out.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{color}"/>')
            elif run:
                out.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(run)}"/>')
            run = []
        return out

    grid = []
    for value in (low, (low + high) / 2, high):
        y = point(0, value).split(",")[1]
        grid.append(f'<line x1="{left}" x2="{width - right}" y1="{y}" y2="{y}" stroke="#27272a"/>'
                    f'<text x="{left - 6}" y="{y}" fill="#a1a1aa" font-size="11" text-anchor="end" '
                    f'dominant-baseline="middle">{value:.1f}%</text>')
    labels = [f'<text x="{left}" y="{height - 8}" fill="#a1a1aa" font-size="11">{_date(weeks[0].week_start_ms)}</text>',
              f'<text x="{width - right}" y="{height - 8}" fill="#a1a1aa" font-size="11" '
              f'text-anchor="end">{_date(weeks[-1].week_start_ms)}</text>']
    body = [*grid, *labels,
            *lines([w.baseline_frr_util_apr_net_pct for w in weeks], _BASELINE_COLOR),
            *lines([w.realized_apr_net_pct for w in weeks], _BOT_COLOR)]
    return f'<svg viewBox="0 0 {width} {height}" role="img">{"".join(body)}</svg>'


def render_page(weeks: Sequence[ProofWeek], *, as_of: datetime, lang: str) -> bytes:
    t = TEXT[lang]
    e = html.escape
    stats = proof_stats(weeks)
    csv_prefix = "" if lang == "zh-TW" else "../"
    if weeks:
        rows = "".join(
            f"<tr><td>{_date(w.week_start_ms)}</td><td>{_fmt_pct(w.realized_apr_net_pct)}</td>"
            f"<td>{_fmt_pct(w.baseline_frr_util_apr_net_pct)}</td></tr>" for w in reversed(weeks))
        results = f"""
<section class="cards">
<div class="card"><div class="muted small">{e(t["stat_realized"].format(weeks=TRAILING_WEEKS))}</div>
<p class="stat">{_fmt_pct(stats.avg_realized_apr_pct)}</p></div>
<div class="card"><div class="muted small">{e(t["stat_spread"])}</div>
<p class="stat">{_fmt_pct(stats.spread_pct, signed=True)}</p></div>
<div class="card"><div class="muted small">{e(t["stat_weeks"])}</div>
<p class="stat">{stats.weeks_tracked}</p></div>
</section>
<section class="panel"><h2>{e(t["chart_title"])}</h2><p class="muted small">{e(t["chart_subtitle"])}</p>
{_chart(weeks)}
<p class="legend small"><span style="color:{_BOT_COLOR}">━ {e(t["bot_line"])}</span>
<span style="color:{_BASELINE_COLOR}">━ {e(t["baseline_line"])}</span></p>
<details><summary>{e(t["table"])}</summary><table><thead><tr><th>{e(t["col_week"])}</th>
<th>{e(t["bot_line"])}</th><th>{e(t["baseline_line"])}</th></tr></thead><tbody>{rows}</tbody></table></details>
</section>"""
    else:
        results = f'<section class="panel muted">{e(t["empty"])}</section>'
    downloads = "".join(
        f'<a class="button" href="{csv_prefix}funding-rates-{s}.csv" download>{e(t["csv_cta"])} ({s})</a>'
        for s in CSV_SYMBOLS)
    page = f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(t["title"])}</title><meta name="description" content="{e(t["subtitle"])}">
<style>{_STYLE}</style></head><body>
<a class="lang small" href="{t["other_href"]}">{e(t["other_lang"])}</a>
<main>
<header><span class="badge">{e(t["badge"])}</span><h1>{e(t["headline"])}</h1>
<p class="muted">{e(t["subtitle"])}</p>
<p class="muted small">{e(t["as_of"].format(date=as_of.astimezone(UTC).strftime("%Y-%m-%d")))}</p></header>
{results}
<section class="panel"><h2>{e(t["csv_title"])}</h2><p class="muted small">{e(t["csv_body"])}</p>{downloads}</section>
<section class="panel"><h2>{e(t["disclaimer_title"])}</h2><p class="muted small">{e(t["disclaimer_body"])}</p></section>
</main></body></html>
"""
    return page.encode()


def render_site(
    weeks: Sequence[ProofWeek], csv_rows: Mapping[str, Iterable[tuple[int, float]]], *,
    as_of: datetime, generated_at: datetime,
) -> dict[str, bytes]:
    """Every file in SITE_FILES, by published path."""
    files = {
        "index.html": render_page(weeks, as_of=as_of, lang="zh-TW"),
        "en/index.html": render_page(weeks, as_of=as_of, lang="en"),
        "proof-summary.json": proof_summary_json(weeks, as_of=as_of, generated_at=generated_at),
        **{f"funding-rates-{s}.csv": funding_rates_csv(csv_rows[s]) for s in CSV_SYMBOLS},
        "_headers": HEADERS.encode(),
    }
    assert tuple(files) == SITE_FILES
    return files
