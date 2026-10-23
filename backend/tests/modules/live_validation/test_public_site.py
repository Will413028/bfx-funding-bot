"""modules/live_validation/public_site.py -- the static proof site's files.

Compliance-critical: no rendered file may carry an absolute-dollar figure or a
promise of returns (the module docstring's red line), and the uploader publishes
exactly SITE_FILES.
"""
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.live_validation.public_site import (
    HEADERS,
    SITE_FILES,
    ProofWeek,
    aggregate_weeks,
    funding_rates_csv,
    proof_stats,
    render_site,
)
from scripts.build_public_site import write_site

ROOT = Path(__file__).resolve().parents[4]
WEEK = 604_800_000
WEEK_1 = 1_782_691_200_000  # 2026-06-29 UTC Monday
AS_OF = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)


@dataclass
class Row:
    week_start_ms: int
    net_interest_usdt: Decimal
    capital_days: Decimal
    baseline_frr_util_apr_net_pct: Decimal | None


def _site(rows: list[Row]) -> dict[str, bytes]:
    csv = {"fUST": [(WEEK_1, 0.0002)], "fUSD": [(WEEK_1, 0.0003)]}
    return render_site(aggregate_weeks(rows), csv, as_of=AS_OF, generated_at=NOW)


def test_realized_apr_is_weighted_by_capital_days_across_cells() -> None:
    # cell a: 1 / 1000 capital-days, cell b: 3 / 1000 -> 4 / 2000 * 365 * 100 = 73%.
    weeks = aggregate_weeks([
        Row(WEEK_1 + WEEK, Decimal("5"), Decimal("1000"), None),
        Row(WEEK_1, Decimal("1"), Decimal("1000"), Decimal("10")),
        Row(WEEK_1, Decimal("3"), Decimal("1000"), Decimal("20")),
    ])
    assert [w.week_start_ms for w in weeks] == [WEEK_1, WEEK_1 + WEEK]
    assert weeks[0].realized_apr_net_pct == Decimal("73")
    assert weeks[0].baseline_frr_util_apr_net_pct == Decimal("15")
    assert weeks[1].baseline_frr_util_apr_net_pct is None


def test_a_week_without_capital_days_has_no_realized_apr_but_keeps_its_baseline() -> None:
    [week] = aggregate_weeks([Row(WEEK_1, Decimal("0"), Decimal("0"), Decimal("8"))])
    assert week.realized_apr_net_pct is None
    assert week.baseline_frr_util_apr_net_pct == Decimal("8")


def test_stats_blend_only_the_trailing_window_but_count_every_week() -> None:
    weeks = [ProofWeek(WEEK_1 + i * WEEK, Decimal(i), Decimal(1)) for i in range(14)]
    weeks.append(ProofWeek(WEEK_1 + 14 * WEEK, None, None))
    stats = proof_stats(weeks, window=3)
    assert stats.avg_realized_apr_pct == Decimal("12.5")  # weeks 12 and 13; the null week skipped
    assert stats.avg_baseline_apr_pct == Decimal(1)
    assert stats.spread_pct == Decimal("11.5")
    assert stats.weeks_tracked == 15


def test_stats_without_data_are_none_not_a_division_error() -> None:
    stats = proof_stats([ProofWeek(WEEK_1, None, None)])
    assert (stats.avg_realized_apr_pct, stats.spread_pct, stats.weeks_tracked) == (None, None, 1)


def test_the_site_is_exactly_the_files_the_uploader_accepts() -> None:
    assert tuple(_site([])) == SITE_FILES
    spec = importlib.util.spec_from_file_location("_bfx_public_site", ROOT / "deploy/vm/ops/bfx_public_site.py")
    assert spec is not None and spec.loader is not None
    uploader = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = uploader
    spec.loader.exec_module(uploader)
    assert uploader.SITE_FILES == SITE_FILES


def test_no_file_carries_a_dollar_figure() -> None:
    # Distinctive amounts: a leak of either weight would show up verbatim.
    # Both branches of each week: one with a baseline, one without.
    site = _site([Row(WEEK_1, Decimal("98765.4321"), Decimal("1234567.89"), Decimal("7.5")),
                  Row(WEEK_1 + WEEK, Decimal("98765.4321"), Decimal("1234567.89"), None)])
    for name, body in site.items():
        text = body.decode()
        for leak in ("98765", "1234567", "capital_days", "net_interest", "gross_interest", "usdt", "$"):
            assert leak not in text.lower(), (name, leak)


def test_every_page_disclaims_future_returns_in_its_language() -> None:
    site = _site([Row(WEEK_1, Decimal("1"), Decimal("1000"), Decimal("7"))])
    zh, en = site["index.html"].decode(), site["en/index.html"].decode()
    assert "並非對未來報酬的預測、承諾或保證" in zh and "不構成投資建議" in zh
    assert "not a projection, prediction, promise, or guarantee of future returns" in en
    assert "investment advice" in en
    for page in (zh, en):
        assert "<script" not in page
        for promise in ("guaranteed return", "保證獲利", "穩賺", "risk-free", "無風險"):
            assert promise not in page.lower()


def test_pages_show_the_trailing_stats_chart_and_both_downloads() -> None:
    site = _site([Row(WEEK_1, Decimal("1"), Decimal("1000"), Decimal("30")),
                  Row(WEEK_1 + WEEK, Decimal("1"), Decimal("1000"), Decimal("30"))])
    zh, en = site["index.html"].decode(), site["en/index.html"].decode()
    assert "36.50%" in zh and "+6.50%" in zh and "<polyline" in zh
    assert 'href="funding-rates-fUST.csv"' in zh and 'href="../funding-rates-fUSD.csv"' in en
    assert 'href="en/"' in zh and 'href="../"' in en
    # Each CSV names its last hour, so a symbol whose ingest stopped shows it.
    assert "(fUST, 資料到 2026-06-29)" in zh and "(fUSD, data through 2026-06-29)" in en
    assert "資料截至 2026-10-19" in zh and "Data as of 2026-10-19" in en


def test_a_site_without_weeks_says_so_and_draws_no_chart() -> None:
    zh = _site([])["index.html"].decode()
    assert "尚無績效資料" in zh and "<svg" not in zh


def test_a_single_week_is_a_point_not_an_empty_line() -> None:
    zh = _site([Row(WEEK_1, Decimal("1"), Decimal("1000"), None)])["index.html"].decode()
    assert "<circle" in zh and "<polyline" not in zh


def test_proof_summary_json_is_snake_case_weeks_with_the_build_time() -> None:
    # Not the API's envelope (`data`, camelCase): the static file has no consumer but the
    # uploader's freshness check and the daily public-site check.
    body = json.loads(_site([Row(WEEK_1, Decimal("1"), Decimal("1000"), Decimal("7"))])["proof-summary.json"])
    assert body == {
        "weeks": [{"week_start_ms": WEEK_1, "realized_apr_net_pct": "36.5",
                   "baseline_frr_util_apr_net_pct": "7"}],
        "as_of": AS_OF.isoformat(), "generated_at": NOW.isoformat(),
    }


def test_csv_is_hourly_close_as_apr_percent() -> None:
    assert funding_rates_csv([(WEEK_1, 0.0002), (WEEK_1 + 3_600_000, 0.0001)]) == (
        b"date,close_apr_pct\n2026-06-29T00:00:00Z,7.300000\n2026-06-29T01:00:00Z,3.650000\n")


def test_headers_forbid_script_and_send_csvs_as_downloads() -> None:
    assert "default-src 'none'" in HEADERS and "script-src" not in HEADERS
    assert "frame-ancestors 'none'" in HEADERS
    assert HEADERS.count("Content-Disposition: attachment") == 2


def test_write_site_swaps_the_whole_directory(tmp_path: Path) -> None:
    out = tmp_path / "public-site"
    write_site(out, {"index.html": b"old", "en/index.html": b"old", "stale.txt": b"x"})
    write_site(out, {"index.html": b"new", "en/index.html": b"new"})
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "en/index.html", "index.html"]
    assert (out / "index.html").read_bytes() == b"new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["public-site"]
