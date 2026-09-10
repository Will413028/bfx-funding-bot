"""Versioned lossless row encoding, independent of canonical event/replay hashes.

Every value, including containers and ordinary JSON, carries a type tag. User
objects therefore cannot impersonate codec metadata. Timestamp ISO offsets and
Decimal exponents are retained; timezone database names are not SQL values.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

FORMAT_VERSION = 1


def _encode(value: object) -> object:
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", value]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, UUID):
        return ["uuid", str(value)]
    if isinstance(value, Decimal) and value.is_finite():
        return ["decimal", str(value)]
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return ["datetime", value.isoformat()]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("archive object keys must be strings")
        return ["map", {key: _encode(item) for key, item in value.items()}]
    if isinstance(value, list):
        return ["list", [_encode(item) for item in value]]
    raise ValueError("unsupported archive value type or representation")


def encode_row(values: Mapping[str, object]) -> bytes:
    if not isinstance(values, Mapping):
        raise ValueError("archive row must be an object")
    return json.dumps(
        {"version": FORMAT_VERSION, "row": _encode(values)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode(node: object) -> object:
    if not isinstance(node, list) or not node:
        raise ValueError("invalid archive type tag")
    if node == ["null"]:
        return None
    if len(node) != 2:
        raise ValueError("invalid archive typed value")
    tag, value = node
    if tag == "bool" and type(value) is bool:
        return value
    if tag == "int" and type(value) is int:
        return value
    if tag == "str" and isinstance(value, str):
        return value
    if tag == "uuid" and isinstance(value, str):
        return UUID(value)
    if tag == "decimal" and isinstance(value, str):
        try:
            decimal = Decimal(value)
        except InvalidOperation:
            raise ValueError("invalid archive decimal") from None
        if decimal.is_finite():
            return decimal
    if tag == "datetime" and isinstance(value, str):
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is not None:
            return stamp
    if tag == "map" and isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items()}
    if tag == "list" and isinstance(value, list):
        return [_decode(item) for item in value]
    raise ValueError("unknown archive tag or invalid typed value")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate archive object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("non-finite JSON number")


def decode_row(payload: bytes) -> dict[str, object]:
    envelope = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"version", "row"}
        or type(envelope["version"]) is not int
        or envelope["version"] != FORMAT_VERSION
    ):
        raise ValueError("invalid archive envelope or version")
    row = _decode(envelope["row"])
    if not isinstance(row, dict):
        raise ValueError("archive row must be an object")
    return row


def row_digest(values: Mapping[str, object]) -> str:
    return hashlib.sha256(encode_row(values)).hexdigest()
