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
from bfx_funding_bot.modules.live_validation.tables import FundingCreditHistoryRow

H = 3_600_000
T0 = 1_768_003_200_000  # 2026-01-10T00:00:00Z
RATE = Decimal("0.0002")


def _credit(credit_id: int, kind: str, held_h: int, period_days: int = 2) -> EndedCredit:
    return EndedCredit(credit_id=credit_id, kind=kind, symbol="fUST", rate=RATE,
                       period_days=period_days,
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
    assert set(symbol["summaries"]) == {"credit_2d", "loan_2d"}
    assert (symbol["summaries"]["credit_2d"]["n"],
            symbol["summaries"]["credit_2d"]["n_early"]) == (2, 1)
    assert symbol["summaries"]["loan_2d"]["n_early"] == 0
    # One of two credits held to term while the rule predicted an immediate repayment.
    assert abs(Decimal(symbol["summaries"]["credit_2d"]["position"]) - Decimal("0.5")) < Decimal("1e-9")
    # The loan opened together with a same-shaped credit: one lend listed twice.
    assert symbol["loans_that_look_converted"] == 1
    assert symbol["credits"][1]["held_hours"] == "1.0000"
    assert symbol["summaries"]["credit_2d"]["median_held_hours"] == "24.5000"
    assert [c["credit_id"] for c in symbol["credits"]] == [1, 2, 3]
    assert symbol["credits"][1]["early"] is True
    markdown = out.read_text()
    assert "## fUST — 3 ended credits (2 rows without a close time left out)" in markdown
    assert "| loan | 2 | 1 | 0 |" in markdown
    # The market is under the trigger from the first close: the 48h credit saw it and stayed,
    # the 1h credit ended at that same close, so it did not see a drop before it ended.
    assert symbol["repaid_after_drop"] == {
        "credit_2d": {"n_dropped": 1, "n_repaid_after": 0, "share": "0"}}
    assert "| 1 | 0 (0.000) |" in markdown
    assert symbol["rate_gap"]["n_long"] == 0 and symbol["rate_gap"]["gap"] is None
    assert "Rate gap, 7-day against 2-day: not reported (0 ended 7-day credits" in markdown
    record = json.loads(out.with_suffix(".run.json").read_text())
    assert record["registry_entry"] == "hold-times"


def test_each_term_gets_its_own_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _data()
    data.credits.append(_credit(4, "credit", 84, period_days=7))
    rc, out = _run(tmp_path, monkeypatch, [data])
    assert rc == 0
    symbol = json.loads(out.with_suffix(".json").read_text())["symbols"][0]
    assert set(symbol["summaries"]) == {"credit_2d", "credit_7d", "loan_2d"}
    assert symbol["summaries"]["credit_2d"]["n"] == 2
    assert symbol["summaries"]["credit_7d"]["median_held_share"] == "0.5"
    assert set(symbol["repaid_after_drop"]) == {"credit_2d", "credit_7d"}
    # One ended 7-day credit is far under the sample the gap is reported from.
    assert (symbol["rate_gap"]["n_long"], symbol["rate_gap"]["n_included"]) == (1, 1)
    assert symbol["rate_gap"]["gap"] is None
    assert "| credit | 7 | 1 |" in out.read_text()
    record = json.loads(out.with_suffix(".run.json").read_text())
    assert record["params"]["long_period_days"] == 7


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


def _row(credit_id: int, account: UUID, *, closed: int | None, amount: str = "-100") -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=account, kind="credit", credit_id=credit_id, symbol="fUST", side=1,
        mts_create=T0, mts_update=T0, amount=Decimal(amount), status="CLOSED", rate=RATE,
        period_days=2, mts_opening=T0 + H, mts_last_payout=closed)


def test_rows_become_ended_credits_of_one_account() -> None:
    one, two = UUID(int=1), UUID(int=2)
    credits, open_rows, account = report.credits_from_rows(
        [_row(1, one, closed=T0 + 5 * H), _row(2, one, closed=None)], "fUST")
    assert (len(credits), open_rows, account) == (1, 1, one)
    assert (credits[0].amount, credits[0].opened_mts, credits[0].closed_mts) == (
        Decimal("100"), T0 + H, T0 + 5 * H)
    # A sample pooled over accounts is refused, whichever rows are still open.
    with pytest.raises(SystemExit, match="2 exchange accounts"):
        report.credits_from_rows([_row(1, one, closed=T0 + 5 * H), _row(2, two, closed=None)], "fUST")
    assert report.credits_from_rows([], "fUST") == ([], 0, None)


def test_one_report_covers_one_account_across_symbols_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = UUID(int=1), UUID(int=2)

    def loader(accounts: list[UUID]):  # type-free helper: returns the stubbed _load
        async def load(symbols: list[str], account_id: UUID | None) -> list[report.SymbolData]:
            return [report.SymbolData(sym, _data().credits, 0, _data().reference, acc)
                    for sym, acc in zip(symbols, accounts, strict=True)]
        return load

    out = tmp_path / "hold.md"
    argv = ["--registry-entry", "x", "--symbols", "fUSD,fUST", "--output", str(out)]
    monkeypatch.setattr(report, "_load", loader([one, two]))
    with pytest.raises(SystemExit, match="2 exchange accounts"):
        asyncio.run(report._amain(argv))
    monkeypatch.setattr(report, "_load", loader([one, one]))
    assert asyncio.run(report._amain(argv)) == 0
    payload = json.loads(out.with_suffix(".json").read_text())
    assert payload["symbols"][0]["exchange_account_id"] == str(one)
    record = json.loads(out.with_suffix(".run.json").read_text())
    assert str(one) in json.dumps(record)


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
