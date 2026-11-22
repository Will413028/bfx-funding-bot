"""Run record: what one research run executed, written next to its report.

A report alone says what the numbers were. The record says what produced them, so a
run can be repeated and two runs compared: the registry entry it tests, the code
version, the full parameters, a fingerprint of every input and of every output file.

Two fingerprints answer two different questions:

* ``config_sha256`` covers the registry entry and the parameters only. Two runs with
  the same value tested the same configuration, so a re-run is not a new trial.
* each input's ``sha256`` covers the data. The same configuration over different data
  (a longer tape, re-ingested candles) shows up as a changed input, not a new config.

The module does no clock, environment or git access: the caller passes the code
version and the times, so a record is a pure function of its arguments.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle

SCHEMA_VERSION = 1
_CHUNK = 1 << 20


@dataclass(frozen=True)
class InputFingerprint:
    """One input of a run: what it was, how much of it, and a content hash."""

    name: str
    kind: str  # "files" | "candles"
    sha256: str
    items: int
    first: str | None
    last: str | None


@dataclass(frozen=True)
class OutputFile:
    name: str
    sha256: str


@dataclass(frozen=True)
class RunRecord:
    schema_version: int
    registry_entry: str
    runner: str
    code_version: str
    started_at: str
    finished_at: str
    params: dict[str, object]
    config_sha256: str
    inputs: tuple[InputFingerprint, ...]
    outputs: tuple[OutputFile, ...]


def _decimal_text(value: Decimal) -> str:
    """One text per numeric value: 0.02, 0.020 and 2E-2 are the same parameter."""
    if not value.is_finite():
        raise ValueError(f"non-finite decimal in a run record: {value}")
    return "0" if value == 0 else format(value.normalize(), "f")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fingerprint_files(name: str, root: Path, pattern: str) -> InputFingerprint:
    """Hash every file under ``root`` matching ``pattern``: relative path and bytes, in path order.

    ``first`` and ``last`` are the first and last relative paths, which for day files
    named by date is the covered range. No matching file is an error: a run that read
    nothing must not leave a record that looks like it read something.

    The pattern must select exactly what the run read (for day files ``fUSD/*.csv.gz``,
    not ``**/*``): a report or record written under ``root`` would otherwise be hashed
    as input by the next run.
    """
    paths = sorted(p for p in root.glob(pattern) if p.is_file())
    if not paths:
        raise ValueError(f"input {name!r}: no file matches {pattern!r} under {root}")
    h = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        h.update(relative.encode("utf-8"))
        h.update(b"\0")
        h.update(bytes.fromhex(file_sha256(path)))
    return InputFingerprint(
        name=name, kind="files", sha256=h.hexdigest(), items=len(paths),
        first=paths[0].relative_to(root).as_posix(), last=paths[-1].relative_to(root).as_posix(),
    )


def fingerprint_candles(name: str, candles: Sequence[FundingCandle]) -> InputFingerprint:
    """Hash one candle series as loaded (the DB is mutable, so the rows are the input).

    The series must be in strictly increasing ``mts`` order, which is what the backtest
    reads; numbers are hashed by value, so a DB NUMERIC and a fixture hash the same.
    """
    if not candles:
        raise ValueError(f"input {name!r}: empty candle series")
    if any(b.mts <= a.mts for a, b in itertools.pairwise(candles)):
        raise ValueError(f"input {name!r}: candles are not in strictly increasing mts order")
    h = hashlib.sha256()
    for c in candles:
        numbers = (c.open, c.close, c.high, c.low, c.volume)
        row = [c.symbol, c.timeframe, c.period_agg, str(c.mts),
               *("" if v is None else _decimal_text(v) for v in numbers)]
        h.update("|".join(row).encode("utf-8"))
        h.update(b"\n")
    return InputFingerprint(
        name=name, kind="candles", sha256=h.hexdigest(), items=len(candles),
        first=candles[0].timestamp().isoformat(), last=candles[-1].timestamp().isoformat(),
    )


def _moment_text(value: datetime | date) -> str:
    if not isinstance(value, datetime):
        return value.isoformat()
    if value.tzinfo is None:
        raise ValueError(f"run record parameter is a naive datetime: {value!r}")
    return value.astimezone(UTC).isoformat()


def _plain(value: object) -> object:
    """Reduce a parameter value to JSON types; an unknown type fails instead of guessing."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime | date):
        return _moment_text(value)
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise TypeError(f"run record parameter mapping has a non-string key: {value!r}")
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"run record parameter of unsupported type {type(value).__name__}: {value!r}")


def config_sha256(registry_entry: str, params: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {"registry_entry": registry_entry, "params": _plain(params)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunFacts:
    """Who ran what and when; everything except the parameters and the data."""

    registry_entry: str
    runner: str
    code_version: str
    started_at: datetime
    finished_at: datetime


def build_run_record(
    facts: RunFacts,
    *,
    params: Mapping[str, object],
    inputs: Iterable[InputFingerprint],
    output_files: Iterable[Path],
) -> RunRecord:
    """Assemble a record. ``params`` are the parameters that define the configuration:
    leave out where the report is written and other values that do not change results.
    """
    for label, text in (("registry_entry", facts.registry_entry), ("runner", facts.runner),
                        ("code_version", facts.code_version)):
        if not text or text != text.strip():
            raise ValueError(f"{label} must be non-empty with no surrounding whitespace")
    for label, moment in (("started_at", facts.started_at), ("finished_at", facts.finished_at)):
        if moment.tzinfo is None:
            raise ValueError(f"{label} must be timezone-aware")
    if facts.finished_at < facts.started_at:
        raise ValueError("finished_at is before started_at")
    fingerprints = tuple(inputs)
    if not fingerprints:
        raise ValueError("a run record needs at least one input fingerprint")
    names = [f.name for f in fingerprints]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate input names: {sorted(names)}")
    plain_params = _plain(params)
    if not isinstance(plain_params, dict):
        raise TypeError("params must be a mapping")
    outputs = tuple(OutputFile(name=p.name, sha256=file_sha256(p)) for p in output_files)
    output_names = [o.name for o in outputs]
    if len(set(output_names)) != len(output_names):
        raise ValueError(f"duplicate output file names: {sorted(output_names)}")
    return RunRecord(
        schema_version=SCHEMA_VERSION,
        registry_entry=facts.registry_entry,
        runner=facts.runner,
        code_version=facts.code_version,
        started_at=facts.started_at.isoformat(),
        finished_at=facts.finished_at.isoformat(),
        params=plain_params,
        config_sha256=config_sha256(facts.registry_entry, params),
        inputs=fingerprints,
        outputs=outputs,
    )


def write_run_record(path: Path, record: RunRecord) -> None:
    """Write the record whole or not at all; a re-run to the same path replaces it."""
    text = json.dumps(asdict(record), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)
