"""Digest-only field evidence. Classification never grants apply authority."""

import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from .codec import encode_row, row_digest
from .contracts import Difference


class RowCollector(Protocol):
    """Explicit in-process opt-in; persist raw values only to private evidence."""

    def __call__(
        self,
        table: str,
        before: Sequence[Mapping[str, object]],
        after: Sequence[Mapping[str, object]],
        *,
        key_columns: tuple[str, ...],
    ) -> None: ...


def compare_rows(
    table: str,
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
    *,
    key_columns: tuple[str, ...],
) -> tuple[Difference, ...]:
    if not key_columns or len(set(key_columns)) != len(key_columns):
        raise ValueError("unique nonempty key columns required")

    def index(rows: Sequence[Mapping[str, object]]) -> dict[bytes, Mapping[str, object]]:
        result: dict[bytes, Mapping[str, object]] = {}
        for row in rows:
            if any(column not in row for column in key_columns):
                raise ValueError("missing archive row key")
            # Validate the entire row, even if no difference would be emitted.
            encode_row(row)
            key = encode_row({column: row[column] for column in key_columns})
            if key in result:
                raise ValueError("duplicate archive row key")
            result[key] = row
        return result

    old, new = index(before), index(after)
    differences: list[Difference] = []
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key, {}), new.get(key, {})
        for column in sorted(left.keys() | right.keys()):
            before_digest = row_digest({column: left[column]}) if column in left else None
            after_digest = row_digest({column: right[column]}) if column in right else None
            if before_digest != after_digest:
                differences.append(
                    Difference(
                        table=table,
                        key_digest=hashlib.sha256(key).hexdigest(),
                        column=column,
                        before_digest=before_digest,
                        after_digest=after_digest,
                    )
                )
    return tuple(differences)


def write_private_comparison(
    path: Path,
    table: str,
    before: Sequence[Mapping[str, object]],
    after: Sequence[Mapping[str, object]],
) -> None:
    """Explicit opt-in raw evidence; refuse existing paths, including symlinks."""
    payload = encode_row({"table": table, "before": list(before), "after": list(after)})
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(payload)
