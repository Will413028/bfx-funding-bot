"""The report's command line and its constants; the criteria are tested on PostgreSQL in
tests/integration/test_sim_soak_report.py."""
from __future__ import annotations

import argparse
from decimal import Decimal

import pytest

from scripts import sim_soak_report as report

ACCOUNT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_the_bar_and_the_floor_are_the_numbers_fixed_before_the_soak() -> None:
    """ADR 2026-10-03 D3 and its 2026-10-05 amendment; a change here is an ADR amendment."""
    assert (report.MIN_WINDOW_HOURS, report.MIN_OPERATOR_KILLS) == (24, 1)
    assert (report.KILL_WINDOW_START_HOUR, report.KILL_WINDOW_END_HOUR) == (6, 10)
    assert Decimal("0.99") == report.MIN_ACCEPTED_CYCLE_RATIO
    assert report.MIN_TRADING_HOURS_AFTER_RESUME == 8
    assert (report.FLOOR_ACKED_SUBMITS, report.FLOOR_FILLS, report.FLOOR_INTEREST_PAYMENTS) == (
        1, 1, 1)


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
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        restore_logging: None) -> None:
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


def test_the_result_file_names_the_verdict_and_the_last_generation() -> None:
    from uuid import UUID
    revisions = [{"service_version": "a" * 40}, {"service_version": "b" * 40}]
    criteria = [report.Criterion("d3.revisions", "r", "PASS", {"revisions": revisions}),
                report.Criterion("d3.window_hours", "r", "PASS")]
    window = report.Window(1_000, 2_000)
    document = report.result_document(criteria, account=UUID(ACCOUNT), window=window,
                                      image_digest="sha256:" + "3" * 64)
    assert (document["verdict"], document["last_service_version"], document["image_digest"],
            document["window"]) == ("PASS", "b" * 40, "sha256:" + "3" * 64,
                                    {"since_ms": 1_000, "until_ms": 2_000})
    assert len(document["report_sha256"]) == 64
    assert (document["schema"], document["mode"]) == (2, "soak") and "upgrade" not in document
    failed = report.result_document(
        [*criteria, report.Criterion("floor.fills", "r", "FAIL")], account=UUID(ACCOUNT),
        window=window, image_digest=None)
    assert (failed["verdict"], failed["image_digest"]) == ("FAIL", None)
    assert failed["report_sha256"] != document["report_sha256"]


def test_the_upgrade_result_reads_its_revisions_from_the_path() -> None:
    from uuid import UUID
    path = report.Criterion("upgrade.path", "r", "PASS", {
        "from": "a" * 40, "to": "b" * 40,
        "revisions": [{"service_version": "a" * 40}, {"service_version": "b" * 40}]})
    document = report.result_document([path], account=UUID(ACCOUNT),
                                      window=report.Window(1_000, 2_000), image_digest=None)
    assert (document["mode"], document["last_service_version"], document["upgrade"]) == (
        "upgrade", "b" * 40, {"from": "a" * 40, "to": "b" * 40})


@pytest.mark.parametrize("extra", [["--upgrade-from", "a"], ["--upgrade-to", "b"]])
def test_the_upgrade_revisions_go_together(
        extra: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        report.main(["--exchange-account-id", ACCOUNT, "--since", "2024-01-01T00:00:00Z",
                     "--until", "2024-01-05T00:00:00Z", *extra])
    assert exited.value.code == 2
    assert "go together" in capsys.readouterr().err


SHA_A, SHA_B = "a" * 40, "b" * 40


@pytest.mark.parametrize(("extra", "reason"), [
    (["--upgrade-from", SHA_A, "--upgrade-to", SHA_A], "upgrade_from_equals_to"),
    (["--upgrade-from", "rev-a", "--upgrade-to", SHA_B], "upgrade_revision_not_a_sha"),
    (["--upgrade-from", SHA_A, "--upgrade-to", SHA_B.upper()], "upgrade_revision_not_a_sha"),
    (["--upgrade-from", SHA_A, "--upgrade-to", SHA_B, "--metrics", "gen1.metrics"],
     "metrics_not_read_in_upgrade_mode"),
])
def test_an_unusable_upgrade_is_refused_before_reading(
        extra: list[str], reason: str, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], restore_logging: None) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@127.0.0.1:1/none")
    code = report.main(["--exchange-account-id", ACCOUNT, "--since", "2024-01-01T00:00:00Z",
                        "--until", "2024-01-05T00:00:00Z", *extra])
    assert code == 2 and reason in capsys.readouterr().err
