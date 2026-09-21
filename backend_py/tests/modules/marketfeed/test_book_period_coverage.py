"""Gate A0 — book period coverage aggregation (pure, no DB)."""
from decimal import Decimal

from bfx_funding_bot.modules.marketfeed.book_period_coverage import (
    BookAskSnapshot,
    render_markdown,
    snapshot_from_payload,
    summarize_period_coverage,
)


def _snap(symbol: str, ts: int, asks: list[tuple[str, int, str]]) -> BookAskSnapshot:
    return BookAskSnapshot(
        symbol=symbol,
        captured_at_ms=ts,
        asks=tuple((Decimal(r), p, Decimal(a)) for r, p, a in asks),
    )


def _by_period(result, period):
    return next(c for c in result.coverage if c.period == period)


def test_exact_share_is_not_at_least_share() -> None:
    # period 14 exact in 1/3 snapshots; period >= 14 satisfied by 30 in two more.
    snaps = [
        _snap("fUST", 1, [("0.0002", 2, "1000"), ("0.0003", 14, "500")]),
        _snap("fUST", 2, [("0.0002", 2, "1000"), ("0.0004", 30, "700")]),
        _snap("fUST", 3, [("0.0002", 2, "1000"), ("0.0004", 30, "700")]),
    ]
    (r,) = summarize_period_coverage(snaps, periods=(14,))
    c = _by_period(r, 14)
    assert (c.n_exact, c.exact_share) == (1, Decimal(1) / Decimal(3))
    assert (c.n_at_least, c.at_least_share) == (3, Decimal(1))


def test_depth_sums_levels_within_snapshot_and_medians_across() -> None:
    snaps = [
        _snap("fUST", 1, [("0.0003", 7, "100"), ("0.00031", 7, "200")]),  # depth 300
        _snap("fUST", 2, [("0.0003", 7, "900")]),                          # depth 900
        _snap("fUST", 3, [("0.0003", 7, "600")]),                          # depth 600
    ]
    (r,) = summarize_period_coverage(snaps, periods=(7,))
    c = _by_period(r, 7)
    assert c.depth_median == Decimal("600")
    assert c.depth_p10 == Decimal("300")
    assert c.best_ask_median == Decimal("0.0003")


def test_premium_over_p2_uses_best_ask_and_skips_snapshots_without_p2() -> None:
    snaps = [
        _snap("fUSD", 1, [("0.0002", 2, "1"), ("0.00025", 2, "1"), ("0.0003", 30, "1")]),
        _snap("fUSD", 2, [("0.0003", 30, "1")]),  # no p2 → excluded from premium
    ]
    (r,) = summarize_period_coverage(snaps, periods=(2, 30))
    assert _by_period(r, 30).premium_over_p2_median == Decimal("0.5")
    assert _by_period(r, 2).premium_over_p2_median is None


def test_period_universe_sorted_by_share_then_period() -> None:
    snaps = [
        _snap("fUST", 1, [("0.0002", 2, "1"), ("0.0003", 30, "1")]),
        _snap("fUST", 2, [("0.0002", 2, "1"), ("0.0003", 120, "1")]),
    ]
    (r,) = summarize_period_coverage(snaps)
    assert r.period_universe == (
        (2, Decimal(1)),
        (30, Decimal("0.5")),
        (120, Decimal("0.5")),
    )
    assert (r.first_ms, r.last_ms, r.n_snapshots) == (1, 2, 2)


def test_empty_input_and_askless_snapshots() -> None:
    assert summarize_period_coverage([]) == []
    (r,) = summarize_period_coverage([_snap("fUST", 1, [])], periods=(2,))
    c = _by_period(r, 2)
    assert (c.n_exact, c.exact_share, c.at_least_share) == (0, Decimal(0), Decimal(0))
    assert c.depth_median is None and c.best_ask_median is None
    assert r.period_universe == ()


def test_snapshot_from_payload_parses_asks_only_as_decimal() -> None:
    payload = {
        "asks": [[0.00021, 30, 3, 120000.0], [0.00022, 2, 1, 50000.0]],
        "bids": [[0.00018, 2, 2, 80000.0]],
    }
    snap = snapshot_from_payload(symbol="fUST", captured_at_ms=5, payload=payload)
    assert snap.asks == (
        (Decimal("0.00021"), 30, Decimal("120000.0")),
        (Decimal("0.00022"), 2, Decimal("50000.0")),
    )


def test_render_markdown_lists_symbols_and_formats_missing_as_dash() -> None:
    snaps = [_snap("fUST", 1, [("0.0002", 2, "1000")])]
    md = render_markdown(summarize_period_coverage(snaps, periods=(2, 14)))
    assert "## fUST — 1 snapshots" in md
    assert "| 2 | 1 | 100.0% | 100.0% | 1000 | 1000 | 0.00020000 | - |" in md
    assert "| 14 | 0 | 0.0% | 0.0% | - | - | - | - |" in md
