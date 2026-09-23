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


def test_json_null_has_its_own_singleton_leaf_and_preserves_sql_null_bytes():
    codec = _codec()
    assert hasattr(codec, "JSON_NULL"), "JSON null singleton is missing"
    assert codec.encode_row({"x": None}) == b'{"row":["map",{"x":["null"]}],"version":1}'
    assert (
        codec.encode_row({"x": codec.JSON_NULL})
        == b'{"row":["map",{"x":["json_null"]}],"version":1}'
    )
    decoded = codec.decode_row(codec.encode_row({"sql": None, "json": codec.JSON_NULL}))
    assert decoded["sql"] is None
    assert decoded["json"] is codec.JSON_NULL
    from copy import deepcopy

    assert deepcopy(decoded)["json"] is codec.JSON_NULL


@pytest.mark.parametrize(
    "value",
    [
        "json_null",
        "null",
        ["json_null"],
        {"type": "json_null"},
        {"version": 1, "row": ["json_null"]},
        {"nested": [None, "null", ["json_null"], {"tag": "json_null"}]},
    ],
)
def test_user_json_cannot_impersonate_json_null_leaf(value):
    codec = _codec()
    assert hasattr(codec, "JSON_NULL"), "JSON null singleton is missing"
    encoded = codec.encode_row({"x": value})
    assert encoded != codec.encode_row({"x": codec.JSON_NULL})
    decoded = codec.decode_row(encoded)["x"]
    assert decoded == value
    assert decoded is not codec.JSON_NULL


@pytest.mark.parametrize(
    "node",
    [
        ["json_null", None],
        ["json_null", "null"],
        ["json_null", []],
        ["json_null", {}, None],
        ["JSON_NULL"],
        ["unknown_null"],
        {"tag": "json_null"},
    ],
)
def test_malformed_json_null_and_unknown_tags_fail_closed(node):
    codec = _codec()
    with pytest.raises(ValueError):
        codec.decode_row(json.dumps({"version": 1, "row": ["map", {"x": node}]}).encode())


def test_existing_format_one_value_bytes_remain_unchanged():
    codec = _codec()
    values = {
        "decimal": Decimal("1.2300"),
        "json": {"nested": [None, True, "json_null"]},
        "uuid": UUID(int=1),
        "time": datetime.fromisoformat("2001-02-03T04:05:06+00:00"),
    }
    expected = (
        b'{"row":["map",{"decimal":["decimal","1.2300"],'
        b'"json":["map",{"nested":["list",[["null"],["bool",true],["str","json_null"]]]}],'
        b'"time":["datetime","2001-02-03T04:05:06+00:00"],'
        b'"uuid":["uuid","00000000-0000-0000-0000-000000000001"]}],"version":1}'
    )
    assert codec.encode_row(values) == expected
    assert codec.decode_row(expected) == values
