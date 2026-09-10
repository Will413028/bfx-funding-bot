import importlib
import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pytest


def _codec():
    name = "bfx_funding_bot.modules.execution.projection_cutover.codec"
    assert importlib.util.find_spec(name.rsplit(".", 1)[0]) is not None, "archive codec missing"
    return importlib.import_module(name)


def test_lossless_recursive_round_trip():
    codec = _codec()
    stamp = datetime.fromisoformat("2026-09-10T12:34:56.123456+08:00")
    row = {
        "id": UUID(int=1),
        "amount": Decimal("1.2300"),
        "at": stamp,
        "null": None,
        "json": {
            "type": "decimal",
            "value": "NaN",
            "nested": [True, False, 123, "中文", {"x": None}],
        },
    }
    restored = codec.decode_row(codec.encode_row(row))
    assert restored == row
    assert restored["amount"].as_tuple() == Decimal("1.2300").as_tuple()
    assert restored["at"].isoformat() == "2026-09-10T12:34:56.123456+08:00"
    assert codec.encode_row(row) == codec.encode_row(dict(reversed(list(row.items()))))


@pytest.mark.parametrize(
    "value",
    [
        1.23,
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        datetime(2026, 9, 10),
        {1: "not a string key"},
        (1, 2),
        b"bytes",
    ],
)
def test_rejects_unsupported_values_recursively(value):
    codec = _codec()
    with pytest.raises(ValueError):
        codec.encode_row({"nested": [value]})


@pytest.mark.parametrize(
    "node",
    [
        ["unknown", "x"],
        ["decimal", "NaN"],
        ["decimal", "Infinity"],
        ["datetime", "2026-09-10T00:00:00"],
        ["int", True],
        ["int", 1.2],
        ["bool", 1],
        ["null", "extra"],
        ["uuid", "bad"],
        ["str", 123],
    ],
)
def test_rejects_invalid_typed_payload(node):
    codec = _codec()
    with pytest.raises(ValueError):
        codec.decode_row(json.dumps({"version": 1, "row": ["map", {"x": node}]}).encode())


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"version":2,"row":["map",{}]}',
        b'{"version":true,"row":["map",{}]}',
        b'{"version":1,"row":["map",{}],"extra":0}',
        b'{"version":1,"row":["map",{"x":["int",1],"x":["int",2]}]}',
        b'{"version":1,"row":["map",{"x":["int",NaN]}]}',
        b"\xff",
    ],
)
def test_rejects_malformed_envelope(payload):
    codec = _codec()
    with pytest.raises(ValueError):
        codec.decode_row(payload)
