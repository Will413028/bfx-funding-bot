"""Read a DR measurement the ops scripts publish (``backup.json`` / ``restore.json``).

The release ceremony used to consume these files as receipts that gated real
money; ADR 2026-09-25 D6 removed that gate. The files remain the scripts'
output contract -- the freshness alert and the operator read them -- so the
tests use the same shape checks the consumer applied to tell green evidence
from revoked or invalid evidence. Staleness was the gate's own rule and is not
checked here.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

_KIND = {"rpo_seconds": "backup", "rto_seconds": "restore"}


def read_measurement(path: Path, *, key: str) -> int:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                raise ValueError("invalid evidence file")
            raw = handle.read(65537)

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for name, item in pairs:
                if name in result:
                    raise ValueError("duplicate evidence key")
                result[name] = item
            return result

        value = json.loads(raw, object_pairs_hook=unique)
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{key}_measurement_unavailable") from exc
    if not isinstance(value, dict) or value.get("measured") is not True:
        raise ValueError(f"{key}_unmeasured")
    if value.get("kind") != _KIND.get(key) or type(value.get("schema_version")) is not int \
            or value["schema_version"] != 1:
        raise ValueError(f"{key}_measurement_invalid")
    seconds = value.get(key)
    if isinstance(seconds, bool) or not isinstance(seconds, int | str):
        raise ValueError(f"{key}_measurement_invalid")
    try:
        measured = int(seconds)
    except ValueError as exc:
        raise ValueError(f"{key}_measurement_invalid") from exc
    observed = value.get("observed_at_ms")
    if measured < 0 or type(observed) is not int or observed < 0:
        raise ValueError(f"{key}_measurement_invalid")
    return measured
