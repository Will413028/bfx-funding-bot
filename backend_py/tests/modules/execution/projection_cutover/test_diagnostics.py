import importlib
import stat
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.event_store.tables import ReconcileObservationRow
from scripts.verify_projection_replay import _canonical_projection_row


def _diagnostics():
    name = "bfx_funding_bot.modules.execution.projection_cutover.diagnostics"
    assert importlib.util.find_spec(name.rsplit(".", 1)[0]) is not None, "field diagnostics missing"
    return importlib.import_module(name)


def test_archive_reports_fields_excluded_by_canonical_replay():
    diagnostics = _diagnostics()
    stamp = datetime(2026, 9, 10, tzinfo=UTC)
    before = {"id": 123, "recorded_at": stamp, "symbol": "fUST"}
    after = {**before, "id": 456, "recorded_at": stamp + timedelta(seconds=1)}
    assert _canonical_projection_row(ReconcileObservationRow(**before)) == (
        _canonical_projection_row(ReconcileObservationRow(**after))
    )
    changes = diagnostics.compare_rows(
        "reconcile_observation", [before], [after], key_columns=("symbol",)
    )
    assert [d.column for d in changes] == ["id", "recorded_at"]
    assert all(d.classification == "unexplained" for d in changes)
    assert all(d.before_digest != d.after_digest for d in changes)
    assert "fUST" not in str([asdict(d) for d in changes])


def test_missing_rows_and_missing_fields_are_not_null():
    diagnostics = _diagnostics()
    before = [{"symbol": "fUSD", "amount": None}, {"symbol": "fUST", "amount": None}]
    after = [{"symbol": "fUST"}, {"symbol": "fEUR", "amount": Decimal("0")}]
    changes = diagnostics.compare_rows("position_state", before, after, key_columns=("symbol",))
    assert len(changes) == 5
    assert sum(d.before_digest is None for d in changes) == 2
    assert sum(d.after_digest is None for d in changes) == 3
    assert changes == diagnostics.compare_rows(
        "position_state", before[::-1], after[::-1], key_columns=("symbol",)
    )


@pytest.mark.parametrize(
    "before,after,keys",
    [
        ([{"id": 1}, {"id": 1}], [], ("id",)),
        ([], [{"id": 1}, {"id": 1}], ("id",)),
        ([{"id": 1}], [], ("missing",)),
        ([], [], ()),
        ([], [], ("id", "id")),
    ],
)
def test_rejects_ambiguous_keys(before, after, keys):
    diagnostics = _diagnostics()
    with pytest.raises(ValueError):
        diagnostics.compare_rows("offer_claims", before, after, key_columns=keys)


def test_scale_and_timestamp_representation_changes_are_visible():
    diagnostics = _diagnostics()
    before = {
        "id": 1,
        "amount": Decimal("1.2300"),
        "at": datetime.fromisoformat("2026-09-10T08:00:00+08:00"),
    }
    after = {
        "id": 1,
        "amount": Decimal("1.23"),
        "at": datetime.fromisoformat("2026-09-10T00:00:00+00:00"),
    }
    assert before == after
    changes = diagnostics.compare_rows("offer_claims", [before], [after], key_columns=("id",))
    assert [d.column for d in changes] == ["amount", "at"]


def test_private_details_require_exclusive_mode_0600_file(tmp_path):
    diagnostics = _diagnostics()
    path = tmp_path / "private.json"
    diagnostics.write_private_comparison(path, "position_state", [{"symbol": "fUSD"}], [])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert b"fUSD" in path.read_bytes()
    with pytest.raises(FileExistsError):
        diagnostics.write_private_comparison(path, "position_state", [], [])
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        diagnostics.write_private_comparison(link, "position_state", [], [])
