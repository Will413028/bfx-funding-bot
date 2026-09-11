"""Digest-only field evidence. Classification never grants apply authority."""

import hashlib
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
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


class DifferenceSink(Protocol):
    """Receive deterministic projection differences without row buffering."""

    def begin_table(self, name: str, key_columns: tuple[str, ...]) -> None: ...

    def append(self, difference: Difference) -> None: ...

    def finish_table(self, table_facts: Mapping[str, object]) -> None: ...


def _encoded_rows(
    rows: Iterable[Mapping[str, object]], *, key_columns: tuple[str, ...]
) -> Iterator[tuple[bytes, Mapping[str, object]]]:
    previous_key: bytes | None = None
    for row in rows:
        if any(column not in row for column in key_columns):
            raise ValueError("missing archive row key")
        # Validate the entire physical row before comparing any columns.
        encode_row(row)
        key = encode_row({column: row[column] for column in key_columns})
        if previous_key is not None:
            if key == previous_key:
                raise ValueError("duplicate archive row key")
            if key < previous_key:
                raise ValueError("archive rows are not sorted by encoded key")
        previous_key = key
        yield key, row


def compare_sorted_rows(
    before: Iterable[Mapping[str, object]],
    after: Iterable[Mapping[str, object]],
    *,
    key_columns: tuple[str, ...],
    table: str = "",
) -> Iterator[Difference]:
    """Merge two encoded-key-ordered row streams into per-column changes.

    The two inputs are consumed once, with one row of look-ahead per side.
    Rows must be ordered by the codec bytes of their complete primary key and
    duplicate keys are rejected before their values can be compared.
    """
    if not key_columns or len(set(key_columns)) != len(key_columns):
        raise ValueError("unique nonempty key columns required")

    old_rows = iter(_encoded_rows(before, key_columns=key_columns))
    new_rows = iter(_encoded_rows(after, key_columns=key_columns))
    old = next(old_rows, None)
    new = next(new_rows, None)
    while old is not None or new is not None:
        left: Mapping[str, object]
        right: Mapping[str, object]
        if old is None:
            if new is None:  # pragma: no cover - guarded by the loop condition.
                break
            key, right = new
            left = {}
            new = next(new_rows, None)
        elif new is None or old[0] < new[0]:
            key, left = old
            right = {}
            old = next(old_rows, None)
        else:
            key, left = old
            right = new[1]
            old = next(old_rows, None)
            new = next(new_rows, None)

        for column in sorted(set(left) | set(right)):
            before_digest = row_digest({column: left[column]}) if column in left else None
            after_digest = row_digest({column: right[column]}) if column in right else None
            if before_digest != after_digest:
                yield Difference(
                    table=table,
                    key_digest=hashlib.sha256(key).hexdigest(),
                    column=column,
                    before_digest=before_digest,
                    after_digest=after_digest,
                )


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
