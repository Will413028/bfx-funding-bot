"""The credit hold-time report: grouping by kind, the comparison columns, the run record."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import scripts.report_credit_hold_times as report
from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.hold_times import EndedCredit

H = 3_600_000
T0 = 1_768_003_200_000  # 2026-01-10T00:00:00Z
RATE = Decimal("0.0002")


def _credit(credit_id: int, kind: str, held_h: int) -> EndedCredit:
    return EndedCredit(credit_id=credit_id, kind=kind, symbol="fUST", rate=RATE, period_days=2,
                       amount=Decimal("100"), opened_mts=T0 + H, closed_mts=T0 + H + held_h * H)


def _data(reference: bool = True) -> report.SymbolData:
    # The 2-day market is half the locked rate throughout: the rule repays within the hour.
    candles = [FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=T0 + i * H,
                             close=RATE / 2) for i in range(96)]
    credits = [_credit(1, "credit", 48), _credit(2, "credit", 1), _credit(3, "loan", 48)]
    return report.SymbolData("fUST", credits, open_rows=2, reference=candles if reference else [])


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: list[report.SymbolData]) -> tuple[int, Path]:
    async def load(symbols: list[str], account_id: UUID | None) -> list[report.SymbolData]:
        assert symbols == ["fUST"] and account_id is None
        return data

    monkeypatch.setattr(report, "_load", load)
    out = tmp_path / "hold.md"
    rc = asyncio.run(report._amain(["--registry-entry", "hold-times", "--output", str(out)]))
    return rc, out


def test_report_groups_by_kind_and_places_the_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out = _run(tmp_path, monkeypatch, [_data()])
    assert rc == 0
    payload = json.loads(out.with_suffix(".json").read_text())
    symbol = payload["symbols"][0]
    assert symbol["open_rows_left_out"] == 2
    # Credits and loans are never pooled: one lend may be listed as both.
    assert set(symbol["summaries"]) == {"credit", "loan"}
    assert (symbol["summaries"]["credit"]["n"], symbol["summaries"]["credit"]["n_early"]) == (2, 1)
    assert symbol["summaries"]["loan"]["n_early"] == 0
    # One of two credits held to term while the rule predicted an immediate repayment.
    assert abs(Decimal(symbol["summaries"]["credit"]["position"]) - Decimal("0.5")) < Decimal("1e-9")
    assert symbol["loans_that_look_converted"] == 0
    assert symbol["credits"][1]["held_hours"] == "1.0000"
    assert [c["credit_id"] for c in symbol["credits"]] == [1, 2, 3]
    assert symbol["credits"][1]["early"] is True
    markdown = out.read_text()
    assert "## fUST — 3 ended credits (2 rows without a close time left out)" in markdown
    assert "| loan | 1 | 0 |" in markdown
    record = json.loads(out.with_suffix(".run.json").read_text())
    assert record["registry_entry"] == "hold-times"


def test_no_credits_or_no_candles_is_an_error_not_an_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out = _run(tmp_path, monkeypatch, [report.SymbolData("fUST", [], 0, [])])
    assert rc == 2 and not out.exists()
    rc, out = _run(tmp_path, monkeypatch, [_data(reference=False)])
    assert rc == 2 and not out.exists()


@pytest.mark.parametrize("margin", ["20", "0", "abc"])
def test_a_bad_margin_is_refused(margin: str) -> None:
    good = ["--registry-entry", "x", "--output", "/nonexistent/x.md"]
    assert report._parse_args(good).refinance_margin == "0.2"
    with pytest.raises(SystemExit):
        report._parse_args([*good, "--refinance-margin", margin])


def test_a_sample_pooled_over_accounts_is_refused() -> None:
    one, two = UUID(int=1), UUID(int=2)
    report.single_account([one, one], "fUST")
    with pytest.raises(SystemExit, match="2 exchange accounts"):
        report.single_account([one, two, one], "fUST")


def test_candles_are_loaded_to_the_term_end_of_every_credit() -> None:
    # Repaid after 3h of a 30-day term: the rule still needs the other 30 days of closes.
    credit = EndedCredit(credit_id=1, kind="credit", symbol="fUST", rate=RATE, period_days=30,
                         amount=Decimal("100"), opened_mts=T0, closed_mts=T0 + 3 * H)
    assert report.reference_end_mts([credit]) >= T0 + 30 * 24 * H


def test_a_symbol_without_credits_is_named_not_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def load(symbols: list[str], account_id: UUID | None) -> list[report.SymbolData]:
        return [_data(), report.SymbolData("fUSD", [], 0, [])]

    monkeypatch.setattr(report, "_load", load)
    out = tmp_path / "hold.md"
    with caplog.at_level("WARNING"):
        rc = asyncio.run(report._amain(["--registry-entry", "x", "--symbols", "fUST,fUSD",
                                        "--output", str(out)]))
    assert rc == 0
    assert "fUSD: no ended credits" in caplog.text
