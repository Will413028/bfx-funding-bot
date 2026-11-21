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
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
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


def _file_sha256(path: Path) -> str:
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
    """
    paths = sorted(p for p in root.glob(pattern) if p.is_file())
    if not paths:
        raise ValueError(f"input {name!r}: no file matches {pattern!r} under {root}")
    h = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        h.update(relative.encode("utf-8"))
        h.update(b"\0")
        h.update(bytes.fromhex(_file_sha256(path)))
    return InputFingerprint(
        name=name, kind="files", sha256=h.hexdigest(), items=len(paths),
        first=paths[0].relative_to(root).as_posix(), last=paths[-1].relative_to(root).as_posix(),
    )


def fingerprint_candles(name: str, candles: Sequence[FundingCandle]) -> InputFingerprint:
    """Hash a candle series as loaded (the DB is mutable, so the rows are the input)."""
    if not candles:
        raise ValueError(f"input {name!r}: empty candle series")
    h = hashlib.sha256()
    for c in candles:
        row = (c.symbol, c.timeframe, c.period_agg, c.mts, c.open, c.close, c.high, c.low, c.volume)
        h.update("|".join("" if v is None else str(v) for v in row).encode("utf-8"))
        h.update(b"\n")
    return InputFingerprint(
        name=name, kind="candles", sha256=h.hexdigest(), items=len(candles),
        first=candles[0].timestamp().isoformat(), last=candles[-1].timestamp().isoformat(),
    )


def _plain(value: object) -> object:
    """Reduce a parameter value to JSON types; an unknown type fails instead of guessing."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal | Path):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
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
        if not text.strip():
            raise ValueError(f"{label} must not be empty")
    for label, moment in (("started_at", facts.started_at), ("finished_at", facts.finished_at)):
        if moment.tzinfo is None:
            raise ValueError(f"{label} must be timezone-aware")
    fingerprints = tuple(inputs)
    if not fingerprints:
        raise ValueError("a run record needs at least one input fingerprint")
    names = [f.name for f in fingerprints]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate input names: {sorted(names)}")
    plain_params = _plain(params)
    if not isinstance(plain_params, dict):
        raise TypeError("params must be a mapping")
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
        outputs=tuple(OutputFile(name=p.name, sha256=_file_sha256(p)) for p in output_files),
    )


def write_run_record(path: Path, record: RunRecord) -> None:
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")
