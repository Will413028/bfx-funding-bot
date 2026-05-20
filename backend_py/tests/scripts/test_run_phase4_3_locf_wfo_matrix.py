"""Phase 4.3 WFO sensitivity sweep script smoke test (unit-level).

Tests pure functions only: compute_verdicts and emit_markdown.
No DB / Neon connectivity required.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

from run_phase4_3_locf_wfo_matrix import (  # type: ignore[import-not-found]
    compute_verdicts,
    emit_markdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    cell: str,
    strategy: str,
    budget_hours: int,
    qualifies: bool,
    *,
    eligible: int = 10,
    wins: int = 7,
    win_pct: str = "0.70",
    margin: str = "0.08",
    health_pct: str = "0.90",
) -> dict[str, Any]:
    return {
        "cell": cell,
        "strategy": strategy,
        "budget_hours": budget_hours,
        "qualifies": qualifies,
        "eligible": eligible,
        "wins": wins,
        "win_pct": win_pct,
        "margin": margin,
        "health_pct": health_pct,
    }


def _three_budgets(
    cell: str,
    strategy: str,
    q6: bool,
    q12: bool,
    q24: bool,
) -> list[dict[str, Any]]:
    return [
        _make_result(cell, strategy, 6, q6),
        _make_result(cell, strategy, 12, q12),
        _make_result(cell, strategy, 24, q24),
    ]


# ---------------------------------------------------------------------------
# compute_verdicts
# ---------------------------------------------------------------------------


def test_compute_verdicts_green_when_12h_qualifies() -> None:
    """12h qualifies → GREEN regardless of 6h / 24h."""
    results = _three_budgets("fUSD_p30", "RatePercentileStrategy", q6=False, q12=True, q24=True)
    verdicts = compute_verdicts(results)
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v["verdict"] == "GREEN"
    assert "4.4 canary" in v["action"]
    assert v["q6"] is False
    assert v["q12"] is True
    assert v["q24"] is True


def test_compute_verdicts_green_when_6h_and_12h_qualify() -> None:
    """6h and 12h both qualify → GREEN (12h still qualifies)."""
    results = _three_budgets("fUSD_p30", "MeanReversionStrategy", q6=True, q12=True, q24=True)
    verdicts = compute_verdicts(results)
    assert verdicts[0]["verdict"] == "GREEN"


def test_compute_verdicts_yellow_when_only_24h_qualifies() -> None:
    """12h fails but 24h qualifies → YELLOW."""
    results = _three_budgets("fUST_p30", "RatePercentileStrategy", q6=False, q12=False, q24=True)
    verdicts = compute_verdicts(results)
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v["verdict"] == "YELLOW"
    assert "24h" in v["action"]
    assert "G2" in v["action"]


def test_compute_verdicts_red_when_no_budget_qualifies() -> None:
    """All budgets fail → RED."""
    results = _three_budgets("fUST_p30", "MeanReversionStrategy", q6=False, q12=False, q24=False)
    verdicts = compute_verdicts(results)
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v["verdict"] == "RED"
    assert "disqualify" in v["action"].lower()


def test_compute_verdicts_multiple_pairs() -> None:
    """Multiple (cell, strategy) pairs produce independent verdicts."""
    results = (
        _three_budgets("fUSD_p30", "RatePercentileStrategy", q6=False, q12=True, q24=True)
        + _three_budgets("fUSD_p30", "MeanReversionStrategy", q6=False, q12=False, q24=False)
        + _three_budgets("fUST_p30", "RatePercentileStrategy", q6=False, q12=False, q24=True)
        + _three_budgets("fUST_p30", "MeanReversionStrategy", q6=True, q12=True, q24=True)
    )
    verdicts = compute_verdicts(results)
    assert len(verdicts) == 4

    by_key = {(v["cell"], v["strategy"]): v for v in verdicts}
    assert by_key[("fUSD_p30", "RatePercentileStrategy")]["verdict"] == "GREEN"
    assert by_key[("fUSD_p30", "MeanReversionStrategy")]["verdict"] == "RED"
    assert by_key[("fUST_p30", "RatePercentileStrategy")]["verdict"] == "YELLOW"
    assert by_key[("fUST_p30", "MeanReversionStrategy")]["verdict"] == "GREEN"


def test_compute_verdicts_missing_budget_treated_as_false() -> None:
    """If a budget entry is absent for a pair, it defaults to False."""
    # Only 6h and 12h entries — no 24h
    results = [
        _make_result("fUSD_p30", "RatePercentileStrategy", 6, False),
        _make_result("fUSD_p30", "RatePercentileStrategy", 12, False),
    ]
    verdicts = compute_verdicts(results)
    # 12h=False, 24h missing→False → RED
    assert verdicts[0]["verdict"] == "RED"


def test_compute_verdicts_empty_results() -> None:
    """Empty results list → empty verdicts."""
    assert compute_verdicts([]) == []


# ---------------------------------------------------------------------------
# emit_markdown
# ---------------------------------------------------------------------------


def test_emit_markdown_writes_file(tmp_path: Path) -> None:
    """emit_markdown creates the output file."""
    results = _three_budgets("fUSD_p30", "RatePercentileStrategy", q6=False, q12=True, q24=True)
    verdicts = compute_verdicts(results)
    out = tmp_path / "results.md"
    emit_markdown(results, verdicts, out)
    assert out.exists()


def test_emit_markdown_contains_header(tmp_path: Path) -> None:
    """Output contains the expected H1 title."""
    results = _three_budgets("fUSD_p30", "RatePercentileStrategy", q6=False, q12=True, q24=True)
    verdicts = compute_verdicts(results)
    out = tmp_path / "results.md"
    emit_markdown(results, verdicts, out)
    text = out.read_text()
    assert "Phase 4.3 LOCF Backtest Sensitivity Sweep Results" in text


def test_emit_markdown_contains_per_pair_table(tmp_path: Path) -> None:
    """Per-pair result rows appear in the markdown table."""
    results = _three_budgets("fUST_p30", "MeanReversionStrategy", q6=False, q12=False, q24=True)
    verdicts = compute_verdicts(results)
    out = tmp_path / "results.md"
    emit_markdown(results, verdicts, out)
    text = out.read_text()
    # All three budget rows appear
    assert "6h" in text
    assert "12h" in text
    assert "24h" in text
    assert "fUST_p30" in text
    assert "MeanReversionStrategy" in text


def test_emit_markdown_contains_verdict_section(tmp_path: Path) -> None:
    """Ship gate verdict matrix section appears with correct verdict."""
    results = _three_budgets("fUSD_p30", "RatePercentileStrategy", q6=False, q12=True, q24=True)
    verdicts = compute_verdicts(results)
    out = tmp_path / "results.md"
    emit_markdown(results, verdicts, out)
    text = out.read_text()
    assert "Ship gate verdict" in text
    assert "GREEN" in text


def test_emit_markdown_creates_parent_dirs(tmp_path: Path) -> None:
    """emit_markdown creates intermediate directories."""
    results = _three_budgets("fUSD_p30", "RatePercentileStrategy", q6=True, q12=True, q24=True)
    verdicts = compute_verdicts(results)
    out = tmp_path / "docs" / "research" / "results.md"
    assert not out.parent.exists()
    emit_markdown(results, verdicts, out)
    assert out.exists()


def test_emit_markdown_all_verdicts_represented(tmp_path: Path) -> None:
    """When results include GREEN/YELLOW/RED, all three appear in output."""
    results = (
        _three_budgets("fUSD_p30", "RatePercentileStrategy", q6=False, q12=True, q24=True)
        + _three_budgets("fUST_p30", "RatePercentileStrategy", q6=False, q12=False, q24=True)
        + _three_budgets("fUSD_p30", "MeanReversionStrategy", q6=False, q12=False, q24=False)
    )
    verdicts = compute_verdicts(results)
    out = tmp_path / "results.md"
    emit_markdown(results, verdicts, out)
    text = out.read_text()
    assert "GREEN" in text
    assert "YELLOW" in text
    assert "RED" in text
