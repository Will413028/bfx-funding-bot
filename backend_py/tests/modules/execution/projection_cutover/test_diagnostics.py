import importlib
import stat
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.event_store.tables import ReconcileObservationRow
from bfx_funding_bot.modules.execution.projection_cutover import evidence
from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope, StreamIdentity
from scripts.verify_projection_replay import _canonical_projection_row

ACCOUNT = UUID(int=1)
PHYSICAL_COLUMNS = (
    "account_id", "deployment_environment", "event_seq_fence", "exchange_account_id",
    "id", "n_credits", "n_offers", "observed_at_ms", "realized_usdt", "recorded_at",
    "reserved_usdt", "symbol",
)


class _OneShotRows:
    def __init__(self, rows):
        self._iterator = iter(rows)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def __len__(self):
        raise AssertionError("stream comparator must not ask for len")

    def __getitem__(self, index):
        raise AssertionError(f"stream comparator must not index rows: {index!r}")


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


def test_streaming_missing_row_keeps_every_physical_column():
    diagnostics = _diagnostics()
    before = _OneShotRows([{
        "id": 1, "account_id": str(ACCOUNT), "exchange_account_id": ACCOUNT,
        "deployment_environment": "ci", "symbol": "fUSD",
        "reserved_usdt": Decimal("1"), "realized_usdt": Decimal("0"),
        "n_offers": 0, "n_credits": 0, "observed_at_ms": 1,
        "event_seq_fence": 1, "recorded_at": datetime(2026, 1, 1, tzinfo=UTC),
    }])
    after = _OneShotRows([])

    changes = tuple(diagnostics.compare_sorted_rows(before, after, key_columns=("id",)))

    assert len(changes) == 12
    assert [change.column for change in changes] == list(PHYSICAL_COLUMNS)


def test_streaming_comparator_rejects_duplicate_keys_without_materializing_rows():
    diagnostics = _diagnostics()

    with pytest.raises(ValueError, match="duplicate archive row key"):
        tuple(diagnostics.compare_sorted_rows(
            _OneShotRows([{"id": 1}, {"id": 1}]), _OneShotRows([]), key_columns=("id",)
        ))


def test_streaming_comparator_writes_complete_large_coverage_without_difference_list(tmp_path):
    diagnostics = _diagnostics()
    row_count = 148_768
    writer = evidence.EvidenceWriter.create(
        tmp_path / "diagnostic",
        manifest_fields={
            "kind": evidence.DIAGNOSTIC_KIND,
            "format_version": 2,
            "run_id": UUID(int=2),
            "scope": Scope(ACCOUNT, "ci"),
            "image_digest": "sha256:" + "a" * 64,
            "projector_version": "execution-state-v1",
            "stream": StreamIdentity(0, 0, "b" * 64),
            "record_kind": evidence.DIFFERENCE_RECORD_KIND,
            "tables": ({"name": "reconcile_observation", "count": row_count},),
        },
    )
    seen_columns: Counter[str] = Counter()

    def rows():
        for row_id in range(1, row_count + 1):
            yield {
                "id": f"{row_id:06d}", "account_id": str(ACCOUNT), "exchange_account_id": ACCOUNT,
                "deployment_environment": "ci", "symbol": "fUSD",
                "reserved_usdt": Decimal("1"), "realized_usdt": Decimal("0"),
                "n_offers": 0, "n_credits": 0, "observed_at_ms": row_id,
                "event_seq_fence": row_id,
                "recorded_at": datetime(2026, 1, 1, tzinfo=UTC),
            }

    for difference in diagnostics.compare_sorted_rows(
        rows(), iter(()), key_columns=("id",), table="reconcile_observation"
    ):
        seen_columns[difference.column] += 1
        writer.append(asdict(difference))

    manifest = writer.finish()

    assert manifest.record_count == row_count * len(PHYSICAL_COLUMNS)
    assert seen_columns == Counter(dict.fromkeys(PHYSICAL_COLUMNS, row_count))
