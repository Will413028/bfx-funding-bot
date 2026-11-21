"""A run record identifies the configuration, the data and the outputs of one research run."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.run_record import (
    SCHEMA_VERSION,
    InputFingerprint,
    RunFacts,
    RunRecord,
    build_run_record,
    config_sha256,
    fingerprint_candles,
    fingerprint_files,
    write_run_record,
)

T0 = datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 21, 1, 5, tzinfo=UTC)
AN_INPUT = InputFingerprint(name="tape", kind="files", sha256="ab", items=1, first="a", last="a")


def _tape(root: Path, days: dict[str, bytes]) -> Path:
    for name, body in days.items():
        path = root / "fUSD" / f"{name}.csv.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return root


def _candle(mts: int, close: str) -> FundingCandle:
    return FundingCandle(symbol="fUSD", timeframe="1h", period_agg="p2", mts=mts, close=close)


FACTS = RunFacts(registry_entry="period-arms-replay", runner="run_decision_space_search",
                 code_version="sha-abc", started_at=T0, finished_at=T1)
PARAMS: dict[str, object] = {"offsets": [Decimal("0.02")], "periods": [2, 30]}


def _record(
    facts: RunFacts = FACTS,
    *,
    params: dict[str, object] | None = None,
    inputs: Sequence[InputFingerprint] = (AN_INPUT,),
    output_files: Sequence[Path] = (),
) -> RunRecord:
    return build_run_record(facts, params=PARAMS if params is None else params,
                            inputs=inputs, output_files=output_files)


def test_file_fingerprint_reports_count_and_covered_range(tmp_path: Path) -> None:
    root = _tape(tmp_path, {"2026-01-01": b"a", "2026-01-03": b"c", "2026-01-02": b"b"})
    fp = fingerprint_files("tape", root, "fUSD/*.csv.gz")
    assert (fp.kind, fp.items) == ("files", 3)
    assert (fp.first, fp.last) == ("fUSD/2026-01-01.csv.gz", "fUSD/2026-01-03.csv.gz")


def test_file_fingerprint_changes_when_a_byte_changes(tmp_path: Path) -> None:
    before = fingerprint_files("tape", _tape(tmp_path / "a", {"2026-01-01": b"x"}), "fUSD/*")
    after = fingerprint_files("tape", _tape(tmp_path / "b", {"2026-01-01": b"y"}), "fUSD/*")
    assert before.sha256 != after.sha256


def test_file_fingerprint_changes_when_content_moves_to_another_day(tmp_path: Path) -> None:
    before = fingerprint_files("tape", _tape(tmp_path / "a", {"2026-01-01": b"x"}), "fUSD/*")
    after = fingerprint_files("tape", _tape(tmp_path / "b", {"2026-01-02": b"x"}), "fUSD/*")
    assert before.sha256 != after.sha256


def test_file_fingerprint_is_the_same_for_the_same_tree_elsewhere(tmp_path: Path) -> None:
    days = {"2026-01-01": b"x", "2026-01-02": b"y"}
    one = fingerprint_files("tape", _tape(tmp_path / "a", days), "fUSD/*")
    two = fingerprint_files("tape", _tape(tmp_path / "b", days), "fUSD/*")
    assert one == two


def test_file_fingerprint_refuses_an_input_with_no_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no file matches"):
        fingerprint_files("tape", tmp_path, "fUSD/*.csv.gz")


def test_candle_fingerprint_changes_with_a_value_and_reports_the_range() -> None:
    base = [_candle(1_700_000_000_000, "0.0001"), _candle(1_700_003_600_000, "0.0002")]
    changed = [base[0], _candle(1_700_003_600_000, "0.0003")]
    fp = fingerprint_candles("p2", base)
    assert fp.sha256 != fingerprint_candles("p2", changed).sha256
    assert (fp.kind, fp.items) == ("candles", 2)
    assert (fp.first, fp.last) == (base[0].timestamp().isoformat(), base[1].timestamp().isoformat())


def test_candle_fingerprint_tells_a_missing_value_from_a_zero() -> None:
    assert (fingerprint_candles("p2", [_candle(1, "0")]).sha256
            != fingerprint_candles("p2", [FundingCandle(symbol="fUSD", timeframe="1h",
                                                        period_agg="p2", mts=1)]).sha256)


def test_candle_fingerprint_refuses_an_empty_series() -> None:
    with pytest.raises(ValueError, match="empty candle series"):
        fingerprint_candles("p2", [])


def test_config_hash_ignores_key_order_and_follows_values() -> None:
    a = config_sha256("entry", {"offsets": [Decimal("0.02")], "periods": [2, 30]})
    assert a == config_sha256("entry", {"periods": [2, 30], "offsets": [Decimal("0.02")]})
    assert a != config_sha256("entry", {"offsets": [Decimal("0.05")], "periods": [2, 30]})
    assert a != config_sha256("other-entry", {"offsets": [Decimal("0.02")], "periods": [2, 30]})


def test_a_rerun_keeps_the_config_hash_when_only_time_code_and_data_change() -> None:
    first = _record()
    other_input = InputFingerprint(name="tape", kind="files", sha256="cd", items=2, first="a", last="b")
    rerun = _record(replace(FACTS, code_version="sha-def", started_at=T1), inputs=[other_input])
    assert first.config_sha256 == rerun.config_sha256
    assert first.inputs != rerun.inputs


def test_params_of_an_unsupported_type_fail_instead_of_being_stringified() -> None:
    with pytest.raises(TypeError, match="unsupported type float"):
        _record(params={"offset": 0.02})


@pytest.mark.parametrize(("field", "facts"), [
    ("registry_entry", replace(FACTS, registry_entry="  ")),
    ("runner", replace(FACTS, runner="  ")),
    ("code_version", replace(FACTS, code_version="  ")),
])
def test_an_empty_identity_field_is_refused(field: str, facts: RunFacts) -> None:
    with pytest.raises(ValueError, match=field):
        _record(facts)


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(replace(FACTS, started_at=T0.replace(tzinfo=None)))


def test_a_run_that_finishes_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError, match="before started_at"):
        _record(replace(FACTS, started_at=T1, finished_at=T0))


def test_the_same_number_written_differently_is_the_same_config() -> None:
    a = config_sha256("entry", {"offset": Decimal("0.02"), "floor": Decimal("0")})
    assert a == config_sha256("entry", {"offset": Decimal("0.020"), "floor": Decimal("0.00")})
    assert a == config_sha256("entry", {"offset": Decimal("2E-2"), "floor": Decimal("-0")})


def test_the_same_instant_in_another_timezone_is_the_same_config() -> None:
    elsewhere = T0.astimezone(timezone(timedelta(hours=8)))
    assert config_sha256("entry", {"start": T0}) == config_sha256("entry", {"start": elsewhere})


def test_a_naive_datetime_parameter_is_refused() -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        _record(params={"start": T0.replace(tzinfo=None)})


def test_a_non_string_parameter_key_is_refused_instead_of_merged() -> None:
    with pytest.raises(TypeError, match="non-string key"):
        _record(params={"caps": {2: Decimal("100"), "2": Decimal("500")}})


def test_identity_with_surrounding_whitespace_is_refused() -> None:
    with pytest.raises(ValueError, match="registry_entry"):
        _record(replace(FACTS, registry_entry="period-arms-replay\n"))


def test_two_output_files_with_one_name_are_refused(tmp_path: Path) -> None:
    for folder in ("fUSD", "fUST"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "report.md").write_text(folder)
    with pytest.raises(ValueError, match="duplicate output file names"):
        _record(output_files=[tmp_path / "fUSD" / "report.md", tmp_path / "fUST" / "report.md"])


def test_candle_fingerprint_hashes_numbers_by_value() -> None:
    assert (fingerprint_candles("p2", [_candle(1, "0.0001")]).sha256
            == fingerprint_candles("p2", [_candle(1, "0.00010000")]).sha256
            == fingerprint_candles("p2", [_candle(1, "1E-4")]).sha256)


@pytest.mark.parametrize("order", [(2, 1), (1, 1)])
def test_candle_fingerprint_refuses_rows_that_are_not_strictly_ascending(order: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="strictly increasing mts"):
        fingerprint_candles("p2", [_candle(mts, "0.0001") for mts in order])


def test_writing_leaves_no_partial_file_and_replaces_an_earlier_record(tmp_path: Path) -> None:
    target = tmp_path / "report.run.json"
    write_run_record(target, _record())
    write_run_record(target, _record(replace(FACTS, code_version="sha-def")))
    assert json.loads(target.read_text())["code_version"] == "sha-def"
    assert [p.name for p in tmp_path.iterdir()] == ["report.run.json"]


def test_a_record_without_inputs_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one input"):
        _record(inputs=[])


def test_duplicate_input_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate input names"):
        _record(inputs=[AN_INPUT, AN_INPUT])


def test_written_record_carries_every_fact_and_the_output_hashes(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("numbers")
    record = _record(output_files=[report])
    target = tmp_path / "report.run.json"
    write_run_record(target, record)
    loaded = json.loads(target.read_text())
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["registry_entry"] == "period-arms-replay"
    assert loaded["code_version"] == "sha-abc"
    assert loaded["params"] == {"offsets": ["0.02"], "periods": [2, 30]}
    assert loaded["started_at"] == T0.isoformat()
    assert loaded["inputs"] == [{"name": "tape", "kind": "files", "sha256": "ab", "items": 1,
                                 "first": "a", "last": "a"}]
    assert loaded["outputs"][0]["name"] == "report.md"
    assert len(loaded["outputs"][0]["sha256"]) == 64

    report.write_text("other numbers")
    assert _record(output_files=[report]).outputs[0].sha256 != loaded["outputs"][0]["sha256"]
