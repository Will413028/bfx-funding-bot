from datetime import UTC, datetime, timedelta

from scripts.report_delivery_metrics import LedgerRow, delivery_metrics, render

T0 = datetime(2026, 10, 1, tzinfo=UTC)
WINDOW = {"start": T0, "end": T0 + timedelta(days=28)}


def _row(id: int, outcome: str, *, at: timedelta, took: timedelta = timedelta(minutes=5),
         degraded: bool | None = False, attempt: str | None = None) -> LedgerRow:
    started = T0 + at
    finished = None if outcome == "started" else started + took
    return LedgerRow(id, attempt or f"a{id}", outcome, started, finished,
                     None if outcome == "started" else degraded)


def test_rollbacks_and_degraded_failures_are_the_failures() -> None:
    rows = [
        _row(1, "deployed", at=timedelta(days=1)),
        _row(2, "rolled_back", at=timedelta(days=2), took=timedelta(minutes=12)),
        _row(3, "failed", at=timedelta(days=3), degraded=True),
        _row(4, "failed", at=timedelta(days=4)),  # precheck: running release untouched
        _row(5, "deployed", at=timedelta(days=3, hours=2)),
    ]
    m = delivery_metrics(rows, **WINDOW)
    assert m.changes == 4 and m.untouched == 1
    assert [f.row.id for f in m.failures] == [2, 3]
    assert m.change_failure_rate == 0.5


def test_recovery_runs_until_the_next_deployed_row_ends() -> None:
    rows = [
        _row(1, "failed", at=timedelta(days=1), degraded=True),
        _row(2, "failed", at=timedelta(days=1, hours=1), degraded=False),
        _row(3, "deployed", at=timedelta(days=1, hours=3)),
        _row(4, "rolled_back", at=timedelta(days=2), took=timedelta(minutes=9)),
    ]
    by_id = {f.row.id: f for f in delivery_metrics(rows, **WINDOW).failures}
    # Row 1 ended at +5 min; row 3 ended at 3h + 5 min.
    assert by_id[1].recovery == timedelta(hours=3) and not by_id[1].upper_bound
    assert by_id[4].recovery == timedelta(minutes=9) and by_id[4].upper_bound


def test_a_degraded_failure_with_no_later_deploy_is_not_recovered() -> None:
    m = delivery_metrics([_row(1, "failed", at=timedelta(days=1), degraded=True)], **WINDOW)
    assert m.failures[0].recovery is None
    assert "| not yet recovered | 1 |" in render(m)


def test_rows_before_the_column_are_unclassified_never_guessed() -> None:
    rows = [_row(1, "failed", at=timedelta(days=1), degraded=None),
            _row(2, "deployed", at=timedelta(days=2), degraded=None),
            _row(3, "deployed", at=timedelta(days=3))]
    m = delivery_metrics(rows, **WINDOW)
    assert (m.changes, m.unclassified, m.failures) == (1, 2, [])


def test_an_attempt_without_a_terminal_row_is_interrupted() -> None:
    rows = [_row(1, "started", at=timedelta(days=1), attempt="x"),
            _row(2, "deployed", at=timedelta(days=1), attempt="x"),
            _row(3, "started", at=timedelta(days=2), attempt="y")]
    assert delivery_metrics(rows, **WINDOW).interrupted == 1


def test_only_rows_that_finished_inside_the_window_count() -> None:
    rows = [_row(1, "rolled_back", at=-timedelta(days=1)),
            _row(2, "deployed", at=timedelta(days=30))]
    m = delivery_metrics(rows, **WINDOW)
    assert (m.changes, m.change_failure_rate) == (0, None)
    assert "| change failure rate | — |" in render(m)
