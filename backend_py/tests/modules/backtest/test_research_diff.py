"""Weekly research diff: drift and overtake rules on report sidecars."""
import json
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.research_diff import (
    ArmSnapshot,
    PairSnapshot,
    ReportSnapshot,
    champion_drift,
    consecutive_overtakes,
    parse_oos,
    parse_period_structure,
    render_markdown,
)
from scripts.diff_research_report import main as diff_main


def _ps_payload(*, ann: str, med: str, ci_lo: str, windows: list[str] | None = None) -> dict:
    return {
        "fill_alpha": "5.0",
        "symbols": [{
            "symbol": "fUSD", "n_windows": 48, "data_window": "x..y", "notes": [],
            "arms": {
                "always_2d": {"median_monthly": "0.43", "p25_monthly": "0.37", "annualized_pct": "5.5",
                              "n_windows": 48},
                "always_30d": {"median_monthly": med, "p25_monthly": "0.54", "annualized_pct": ann,
                               "n_windows": 48},
            },
            "series_used": {},
            "pairs": {"always_30d_vs_always_2d": {"mean_active": "0.47", "mean_ci": [ci_lo, "0.61"],
                                                  "information_ratio": "0.9", "pct_months_outperform": "0.9"}},
            "per_year_mean_monthly": {},
            **({"windows": {"always_30d": [[i, w] for i, w in enumerate(windows)]}} if windows else {}),
        }],
    }


def test_parse_period_structure_keys_arms_and_pairs_by_symbol() -> None:
    snap = parse_period_structure(_ps_payload(ann="11.5", med="0.83", ci_lo="0.34"), label="w1")
    assert snap.arms["fUSD:always_30d"].annualized_pct == Decimal("11.5")
    assert snap.pairs["fUSD:always_30d_vs_always_2d"].ci_lo == Decimal("0.34")
    assert snap.arms["fUSD:always_30d"].windows == ()


def test_parse_oos_has_arms_but_no_pairs() -> None:
    payload = {"reports": [{
        "cell": "fUST_a30", "n_windows": 86,
        "strategy": {"median_monthly": "0.57", "p25_monthly": "0.45", "annualized_pct": "7.7"},
        "baseline": {"median_monthly": "0.52", "p25_monthly": "0.40", "annualized_pct": "6.7"},
        "active": {}, "median_ci": ["0.53", "0.63"], "deflated_sharpe": "1.0", "n_trials": 38,
        "windows": {"strategy": [[1, "0.5"], [2, "0.6"]]},
    }]}
    snap = parse_oos(payload, label="w1")
    assert set(snap.arms) == {"fUST_a30:strategy", "fUST_a30:baseline"}
    assert snap.arms["fUST_a30:strategy"].windows == (Decimal("0.5"), Decimal("0.6"))
    assert snap.pairs == {}


def test_champion_drift_needs_windows_and_compares_recent_median_to_p25() -> None:
    healthy = ArmSnapshot("a", Decimal("0.5"), Decimal("0.4"), Decimal("6"),
                          windows=tuple(Decimal("0.5") for _ in range(20)))
    drifting = ArmSnapshot("a", Decimal("0.5"), Decimal("0.4"), Decimal("6"),
                           windows=tuple([Decimal("0.5")] * 8 + [Decimal("0.3")] * 12))
    short = ArmSnapshot("a", Decimal("0.5"), Decimal("0.4"), Decimal("6"), windows=(Decimal("0.1"),) * 5)
    assert champion_drift(healthy) is False
    assert champion_drift(drifting) is True
    assert champion_drift(short) is None
    assert champion_drift(ArmSnapshot("a", Decimal("0.5"), Decimal("0.4"), Decimal("6"))) is None


def _snap(label: str, ci_lo: str) -> ReportSnapshot:
    return ReportSnapshot(label=label, pairs={
        "p": PairSnapshot("p", Decimal("0.4"), Decimal(ci_lo), Decimal("0.6")),
    })


def test_overtake_requires_a_full_consecutive_streak() -> None:
    assert consecutive_overtakes([_snap("1", "0.1"), _snap("2", "0.1")], streak=3) == []
    assert consecutive_overtakes([_snap("1", "0.1"), _snap("2", "-0.1"), _snap("3", "0.1")], streak=3) == []
    assert consecutive_overtakes([_snap("1", "0.1"), _snap("2", "0.2"), _snap("3", "0.1")], streak=3) == ["p"]
    # An older negative report outside the streak window does not matter.
    assert consecutive_overtakes([_snap("0", "-1"), _snap("1", "0.1"), _snap("2", "0.2"), _snap("3", "0.1")]) == ["p"]


def test_render_lists_flags_and_deltas() -> None:
    h = [
        parse_period_structure(_ps_payload(ann="10.0", med="0.80", ci_lo="0.30"), label="w1"),
        parse_period_structure(_ps_payload(ann="10.5", med="0.81", ci_lo="0.32"), label="w2"),
        parse_period_structure(
            _ps_payload(ann="11.5", med="0.83", ci_lo="0.34", windows=["0.9"] * 36 + ["0.2"] * 12),
            label="w3",
        ),
    ]
    md = render_markdown(h, kind="period-structure")
    assert "**Champion drift**: fUSD:always_30d" in md
    assert "**Challenger overtake** (3 consecutive): fUSD:always_30d_vs_always_2d" in md
    assert "| fUSD:always_30d | 11.50 | 1.00 |" in md  # Δ annualized vs w2
    assert "| fUSD:always_30d_vs_always_2d | 0.4700 | [0.3400, 0.6100] | 3 |" in md
    assert "promotes" in md


def test_script_reads_glob_in_filename_order(tmp_path: Path) -> None:
    for i, ci in enumerate(["0.1", "0.2", "0.3"], start=1):
        (tmp_path / f"2026-09-0{i}-period-structure-book.json").write_text(
            json.dumps(_ps_payload(ann="10", med="0.8", ci_lo=ci))
        )
    out = tmp_path / "diff.md"
    rc = diff_main(["--kind", "period-structure", "--glob", str(tmp_path / "*-period-structure-book.json"),
                    "--out", str(out)])
    assert rc == 0
    text = out.read_text()
    assert "**Current**: 2026-09-03-period-structure-book.json" in text
    assert "fUSD:always_30d_vs_always_2d" in text.split("## Flags")[1].split("## Arms")[0]
    assert diff_main(["--kind", "oos", "--glob", str(tmp_path / "nothing-*.json")]) == 1
