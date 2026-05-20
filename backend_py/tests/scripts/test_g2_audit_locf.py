"""Phase 4.3 G2 audit script smoke tests — mocked Axiom, no real network."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Mirror g1 smoke test pattern: insert scripts/ dir onto sys.path
sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

import g2_audit_locf as script_mod  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# AxiomQueryClient._tabular_to_rows (unit)
# ---------------------------------------------------------------------------

def test_tabular_to_rows_basic() -> None:
    """Tabular response flattens to list[dict] correctly."""
    payload = {
        "tables": [{
            "fields": [{"name": "cell"}, {"name": "stale_count_"}],
            "columns": [
                ["fUSD_p30", "fUST_p30"],
                [5, 3],
            ],
        }],
    }
    rows = script_mod.AxiomQueryClient._tabular_to_rows(payload)
    assert rows == [
        {"cell": "fUSD_p30", "stale_count_": 5},
        {"cell": "fUST_p30", "stale_count_": 3},
    ]


def test_tabular_to_rows_empty() -> None:
    assert script_mod.AxiomQueryClient._tabular_to_rows({}) == []
    assert script_mod.AxiomQueryClient._tabular_to_rows({"tables": []}) == []
    assert script_mod.AxiomQueryClient._tabular_to_rows(
        {"tables": [{"fields": [{"name": "x"}], "columns": []}]},
    ) == []


# ---------------------------------------------------------------------------
# APL builder checks
# ---------------------------------------------------------------------------

def test_f1_query_uses_bracket_quoted_payload_fields() -> None:
    q = script_mod.build_f1_stale_signals("evt", "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z")
    assert "['payload.is_stale'] == true" in q
    assert "['payload.strategy']" in q
    # Must NOT use bare payload.is_stale (APL dotted field bug)
    assert "payload.is_stale ==" not in q


def test_f2_query_fresh_signal_filter() -> None:
    q = script_mod.build_f2_fresh_signals("evt", "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z")
    assert "['payload.is_stale'] == false" in q
    assert 'event_type == "signal"' in q


def test_f3_query_uses_stale_exceeded() -> None:
    q = script_mod.build_f3_sparseness_rate("evt", "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z")
    assert '"stale_exceeded"' in q
    assert "health_check" in q
    assert "stale_count_" in q


def test_decisions_query_includes_pnl_proxy() -> None:
    q = script_mod.build_decisions_query("evt", "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z")
    assert "['payload.pnl_proxy']" in q
    assert 'event_type == "decision"' in q


# ---------------------------------------------------------------------------
# _parse_time
# ---------------------------------------------------------------------------

def test_parse_time_valid_z_suffix() -> None:
    t = script_mod._parse_time("2026-05-20T10:00:00Z")
    assert t > 0.0


def test_parse_time_none_or_empty() -> None:
    assert script_mod._parse_time(None) == 0.0
    assert script_mod._parse_time("") == 0.0


# ---------------------------------------------------------------------------
# join_stale_with_decisions
# ---------------------------------------------------------------------------

def test_join_stale_with_decisions_basic() -> None:
    """Stale signal + matching decision within tolerance → joined entry with is_win from pnl_proxy."""
    stale = [{"_time": "2026-05-20T10:00:00Z", "cell": "fUSD_p30",
              "payload.strategy": "rate_percentile"}]
    decisions = [{"_time": "2026-05-20T10:00:30Z", "cell": "fUSD_p30",
                  "payload.strategy": "rate_percentile",
                  "payload.pnl_proxy": 0.005}]
    joined = script_mod.join_stale_with_decisions(stale, decisions)
    assert len(joined) == 1
    assert joined[0]["is_win"] is True
    assert joined[0]["pnl_proxy"] == pytest.approx(0.005)
    assert joined[0]["cell"] == "fUSD_p30"
    assert joined[0]["strategy"] == "rate_percentile"


def test_join_stale_with_decisions_loss() -> None:
    """pnl_proxy <= 0 → is_win False."""
    stale = [{"_time": "2026-05-20T10:00:00Z", "cell": "fUST_p30",
              "payload.strategy": "rate_percentile"}]
    decisions = [{"_time": "2026-05-20T10:00:00Z", "cell": "fUST_p30",
                  "payload.strategy": "rate_percentile",
                  "payload.pnl_proxy": -0.002}]
    joined = script_mod.join_stale_with_decisions(stale, decisions)
    assert len(joined) == 1
    assert joined[0]["is_win"] is False


def test_join_stale_with_decisions_no_match_outside_tolerance() -> None:
    """Decision 120s away → no join."""
    stale = [{"_time": "2026-05-20T10:00:00Z", "cell": "fUSD_p30",
              "payload.strategy": "mean_reversion"}]
    decisions = [{"_time": "2026-05-20T10:02:01Z", "cell": "fUSD_p30",
                  "payload.strategy": "mean_reversion",
                  "payload.pnl_proxy": 0.01}]
    joined = script_mod.join_stale_with_decisions(stale, decisions)
    assert joined == []


def test_join_stale_with_decisions_cell_mismatch() -> None:
    """Different cell → no join."""
    stale = [{"_time": "2026-05-20T10:00:00Z", "cell": "fUSD_p30",
              "payload.strategy": "rate_percentile"}]
    decisions = [{"_time": "2026-05-20T10:00:00Z", "cell": "fUST_p30",
                  "payload.strategy": "rate_percentile",
                  "payload.pnl_proxy": 0.01}]
    joined = script_mod.join_stale_with_decisions(stale, decisions)
    assert joined == []


def test_join_stale_with_decisions_missing_fields_skipped() -> None:
    """Signals missing cell or strategy are silently skipped."""
    stale = [
        {"_time": "2026-05-20T10:00:00Z"},  # no cell
        {"_time": "2026-05-20T10:00:00Z", "cell": "fUSD_p30"},  # no strategy
    ]
    joined = script_mod.join_stale_with_decisions(stale, [])
    assert joined == []


# ---------------------------------------------------------------------------
# compute_verdict
# ---------------------------------------------------------------------------

def test_compute_verdict_no_stale_signals_yellow() -> None:
    """No stale signals → YELLOW (extend window or check F3)."""
    verdict = script_mod.compute_verdict([], "fUSD_p30", "rate_percentile")
    assert verdict["verdict"] == "YELLOW"
    assert verdict["n_stale"] == 0


def test_compute_verdict_low_sample_yellow() -> None:
    """Sample < 30 → YELLOW (extend window)."""
    joined = [
        {"cell": "fUSD_p30", "strategy": "rate_percentile", "is_win": True, "pnl_proxy": 0.001}
        for _ in range(20)
    ]
    verdict = script_mod.compute_verdict(joined, "fUSD_p30", "rate_percentile")
    assert verdict["verdict"] == "YELLOW"
    assert verdict["n_stale"] == 20


def test_compute_verdict_green_when_qualifies() -> None:
    """n>=30, win_pct>=60%, margin>=5% → GREEN."""
    wins = [
        {"cell": "fUSD_p30", "strategy": "rate_percentile", "is_win": True, "pnl_proxy": 0.1}
        for _ in range(40)
    ]
    losses = [
        {"cell": "fUSD_p30", "strategy": "rate_percentile", "is_win": False, "pnl_proxy": -0.001}
        for _ in range(20)
    ]
    verdict = script_mod.compute_verdict(wins + losses, "fUSD_p30", "rate_percentile")
    assert verdict["verdict"] == "GREEN"
    assert 0.65 <= verdict["win_pct"] <= 0.70  # 40/60
    assert verdict["n_stale"] == 60


def test_compute_verdict_yellow_close_to_threshold() -> None:
    """win_pct in [55%, 60%) → YELLOW."""
    wins = [
        {"cell": "fUSD_p30", "strategy": "mean_reversion", "is_win": True, "pnl_proxy": 0.05}
        for _ in range(57)
    ]
    losses = [
        {"cell": "fUSD_p30", "strategy": "mean_reversion", "is_win": False, "pnl_proxy": -0.001}
        for _ in range(43)
    ]
    verdict = script_mod.compute_verdict(wins + losses, "fUSD_p30", "mean_reversion")
    assert verdict["verdict"] == "YELLOW"
    assert verdict["n_stale"] == 100


def test_compute_verdict_red_below_threshold() -> None:
    """win_pct < 55% → RED."""
    wins = [
        {"cell": "fUST_p30", "strategy": "rate_percentile", "is_win": True, "pnl_proxy": 0.001}
        for _ in range(15)
    ]
    losses = [
        {"cell": "fUST_p30", "strategy": "rate_percentile", "is_win": False, "pnl_proxy": -0.001}
        for _ in range(35)
    ]
    verdict = script_mod.compute_verdict(wins + losses, "fUST_p30", "rate_percentile")
    assert verdict["verdict"] == "RED"
    assert verdict["n_stale"] == 50


def test_compute_verdict_filters_to_correct_cell() -> None:
    """Joined list contains other cells — verdict only counts matching pair."""
    mixed = [
        {"cell": "fUSD_p30", "strategy": "rate_percentile", "is_win": True, "pnl_proxy": 0.1}
        for _ in range(40)
    ] + [
        {"cell": "fUST_p30", "strategy": "rate_percentile", "is_win": False, "pnl_proxy": -0.1}
        for _ in range(10)
    ]
    # fUSD_p30 verdict should only count its 40 rows
    v_usd = script_mod.compute_verdict(mixed, "fUSD_p30", "rate_percentile")
    assert v_usd["n_stale"] == 40

    v_ust = script_mod.compute_verdict(mixed, "fUST_p30", "rate_percentile")
    assert v_ust["n_stale"] == 10  # < 30 → YELLOW
    assert v_ust["verdict"] == "YELLOW"


# ---------------------------------------------------------------------------
# emit_markdown
# ---------------------------------------------------------------------------

def test_emit_markdown_structure(tmp_path: Path) -> None:
    """Emitted markdown includes verdict table and sparseness section."""
    verdicts = [
        {
            "cell": "fUSD_p30", "strategy": "rate_percentile",
            "n_stale": 50, "wins": 35, "win_pct": 0.70, "margin": 0.08,
            "verdict": "GREEN", "rationale": "qualifies",
        },
    ]
    sparseness = [{"_time": "2026-05-20", "cell": "fUSD_p30", "stale_count_": 5}]
    output = tmp_path / "g2.md"
    script_mod.emit_markdown(
        verdicts, sparseness,
        "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z",
        output,
    )

    content = output.read_text()
    assert "G2 Calibration Audit" in content
    assert "fUSD_p30" in content
    assert "**GREEN**" in content
    assert "Sparseness rate" in content
    assert "2026-05-20" in content


def test_emit_markdown_empty_sparseness(tmp_path: Path) -> None:
    """No sparseness rows → shows fallback message, no table header."""
    verdicts = [
        {"cell": "fUSD_p30", "strategy": "rate_percentile",
         "n_stale": 0, "verdict": "YELLOW", "rationale": "no stale signals joined"},
    ]
    output = tmp_path / "g2_empty.md"
    script_mod.emit_markdown(verdicts, [], "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z", output)

    content = output.read_text()
    assert "No stale_exceeded" in content
    assert "YELLOW" in content


def test_emit_markdown_creates_parent_dir(tmp_path: Path) -> None:
    """emit_markdown creates parent directory if it does not exist."""
    output = tmp_path / "nested" / "deep" / "report.md"
    assert not output.parent.exists()
    script_mod.emit_markdown([], [], "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z", output)
    assert output.exists()


def test_emit_markdown_all_audit_pairs_three_verdicts(tmp_path: Path) -> None:
    """GREEN + YELLOW + RED all appear in one report."""
    verdicts = [
        {"cell": "fUSD_p30", "strategy": "rate_percentile",
         "n_stale": 50, "wins": 35, "win_pct": 0.70, "margin": 0.08,
         "verdict": "GREEN", "rationale": "qualifies"},
        {"cell": "fUSD_p30", "strategy": "mean_reversion",
         "n_stale": 20, "verdict": "YELLOW", "rationale": "sample < 30"},
        {"cell": "fUST_p30", "strategy": "rate_percentile",
         "n_stale": 50, "wins": 20, "win_pct": 0.40, "margin": -0.01,
         "verdict": "RED", "rationale": "below threshold"},
    ]
    output = tmp_path / "all3.md"
    script_mod.emit_markdown(verdicts, [], "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z", output)
    content = output.read_text()
    assert "**GREEN**" in content
    assert "**YELLOW**" in content
    assert "**RED**" in content


# ---------------------------------------------------------------------------
# run_audit end-to-end (mocked Axiom)
# ---------------------------------------------------------------------------

async def test_run_audit_writes_output_with_mocked_axiom_empty(tmp_path: Path) -> None:
    """All 4 queries return empty → writes valid markdown with 3 YELLOW verdicts."""
    mock_client = AsyncMock(spec=script_mod.AxiomQueryClient)
    mock_client.dataset = "bfx-funding-bot"
    mock_client.query_apl = AsyncMock(side_effect=[[], [], [], []])

    output = tmp_path / "audit.md"
    await script_mod.run_audit(mock_client, "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z", output)

    assert output.exists()
    content = output.read_text()
    assert "G2 Calibration Audit" in content
    # All 3 AUDIT_PAIRS get YELLOW (no joined stale signals)
    assert content.count("YELLOW") >= 3
    assert mock_client.query_apl.call_count == 4


async def test_run_audit_with_stale_and_decision_data(tmp_path: Path) -> None:
    """Stale signals + matching decisions → non-trivial verdicts in report."""
    stale_rows = [
        {"_time": f"2026-05-20T{h:02d}:00:00Z", "cell": "fUSD_p30",
         "payload.strategy": "rate_percentile"}
        for h in range(40)
    ]
    fresh_rows: list[dict] = []
    decision_rows = [
        {"_time": f"2026-05-20T{h:02d}:00:10Z", "cell": "fUSD_p30",
         "payload.strategy": "rate_percentile",
         "payload.pnl_proxy": 0.1 if h < 35 else -0.001}
        for h in range(40)
    ]
    sparseness_rows: list[dict] = []

    mock_client = AsyncMock(spec=script_mod.AxiomQueryClient)
    mock_client.dataset = "bfx-funding-bot"
    # F1 stale, F2 fresh, decisions, F3 sparseness
    mock_client.query_apl = AsyncMock(
        side_effect=[stale_rows, fresh_rows, decision_rows, sparseness_rows],
    )

    output = tmp_path / "audit_with_data.md"
    await script_mod.run_audit(mock_client, "2026-05-20T00:00:00Z", "2026-06-03T00:00:00Z", output)

    content = output.read_text()
    # fUSD_p30 x rate_percentile: 40 signals, 35 wins (87.5% > 60%), margin > 0.05 → GREEN
    assert "**GREEN**" in content
    assert "fUSD_p30" in content
