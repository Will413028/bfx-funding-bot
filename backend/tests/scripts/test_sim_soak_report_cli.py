"""The report's command line and its constants; the criteria are tested on PostgreSQL in
tests/integration/test_sim_soak_report.py."""
from __future__ import annotations

import argparse
from decimal import Decimal

import pytest

from scripts import sim_soak_report as report

ACCOUNT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_the_bar_and_the_floor_are_the_numbers_fixed_before_the_soak() -> None:
    """ADR 2026-10-03 D3 and its 2026-10-04 amendment; a change here is an ADR amendment."""
    assert (report.MIN_WINDOW_HOURS, report.MIN_DEPLOY_RESTARTS, report.MIN_OPERATOR_KILLS) == (
        72, 2, 1)
    assert Decimal("0.99") == report.MIN_ACCEPTED_CYCLE_RATIO
    assert report.MIN_TRADING_HOURS_AFTER_RESUME == 24
    assert (report.FLOOR_ACKED_SUBMITS, report.FLOOR_FILLS, report.FLOOR_CANCELS,
            report.FLOOR_CREDITS_CLOSED_BY_EXPIRY, report.FLOOR_INTEREST_PAYMENTS_PER_DAY) == (
        50, 10, 10, 1, 1)


def test_instants_are_iso_with_an_offset() -> None:
    assert report._instant("2024-01-01T00:00:00Z") == 1_704_067_200_000
    assert report._instant("2024-01-01T02:00:00+02:00") == 1_704_067_200_000
    with pytest.raises(argparse.ArgumentTypeError):
        report._instant("2024-01-01T00:00:00")  # no offset: not an instant
    with pytest.raises(argparse.ArgumentTypeError):
        report._instant("yesterday")


def test_a_window_must_run_forwards() -> None:
    with pytest.raises(report.ReportRefused, match="window_invalid"):
        report.Window(5, 5)


def test_an_unreachable_database_is_a_refusal_that_leaks_nothing(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@127.0.0.1:1/none")
    code = report.main(["--exchange-account-id", ACCOUNT, "--since", "2024-01-01T00:00:00Z",
                        "--until", "2024-01-05T00:00:00Z"])
    captured = capsys.readouterr()
    assert code == 2 and "secret" not in captured.err + captured.out
    assert "report_failed" in captured.err


def test_exit_codes_rank_fail_over_unavailable_over_pass() -> None:
    def c(status: report.Status) -> report.Criterion:
        return report.Criterion("x", "r", status)

    assert report.exit_code([c("PASS"), c("PASS")]) == 0
    assert report.exit_code([c("PASS"), c("UNAVAILABLE")]) == 3
    assert report.exit_code([c("UNAVAILABLE"), c("FAIL")]) == 1
