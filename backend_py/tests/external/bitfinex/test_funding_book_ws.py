from __future__ import annotations

import json
import zlib

from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient


def test_book_subscription_enables_sequence_and_checksum() -> None:
    client = FundingBookWSClient(symbols=("fUST",), length=25)

    frames = client.subscription_frames()

    assert json.loads(frames[0]) == {"event": "conf", "flags": 196608}
    assert json.loads(frames[1]) == {
        "event": "subscribe",
        "channel": "book",
        "symbol": "fUST",
        "prec": "P0",
        "freq": "F0",
        "len": 25,
    }


def test_parser_routes_snapshot_update_checksum_sequence_and_disconnect() -> None:
    received: list[tuple[str, object]] = []
    client = FundingBookWSClient(
        symbols=("fUST",),
        on_snapshot=lambda symbol, levels, sequence: received.append(
            ("snapshot", (symbol, levels, sequence))
        ),
        on_update=lambda symbol, level, sequence: received.append(
            ("update", (symbol, level, sequence))
        ),
        on_checksum=lambda symbol, checksum, sequence: received.append(
            ("checksum", (symbol, checksum, sequence))
        ),
        on_sequence=lambda symbol, sequence: received.append(("sequence", (symbol, sequence))),
        on_disconnect=lambda: received.append(("disconnect", None)),
    )

    client.handle_raw(
        json.dumps({"event": "subscribed", "channel": "book", "chanId": 9, "symbol": "fUST"})
    )
    client.handle_raw(json.dumps([9, [["0.00020", 7, 1, "-100"]], 10]))
    client.handle_raw(json.dumps([9, ["0.00021", 7, 1, "100"], 11]))
    client.handle_raw(json.dumps([9, "cs", 123, 12]))
    client.handle_raw(json.dumps([9, "hb", 13]))
    client.mark_disconnected()

    assert received == [
        ("snapshot", ("fUST", [["0.00020", 7, 1, "-100"]], 10)),
        ("update", ("fUST", ["0.00021", 7, 1, "100"], 11)),
        ("checksum", ("fUST", 123, 12)),
        ("sequence", ("fUST", 13)),
        ("disconnect", None),
    ]


def test_checksum_uses_signed_crc32_for_period_aware_book() -> None:
    client = FundingBookWSClient(symbols=("fUST",))

    checksum = client.checksum(
        bids=[
            ["0.00020", 7, 1, "-100"],
            ["0.00019", 7, 1, "-99"],
        ],
        asks=[
            ["0.00021", 7, 1, "100"],
            ["0.00022", 7, 1, "101"],
        ],
    )

    unsigned = zlib.crc32(b"0.00020:7:-100:0.00021:7:100:0.00019:7:-99:0.00022:7:101")
    expected = unsigned - 2**32 if unsigned >= 2**31 else unsigned
    assert checksum == expected


def test_parser_treats_an_empty_first_book_frame_as_a_snapshot() -> None:
    snapshots: list[tuple[str, list[list[object]], int | None]] = []
    client = FundingBookWSClient(
        symbols=("fUST",),
        on_snapshot=lambda symbol, levels, sequence: snapshots.append((symbol, levels, sequence)),
    )

    client.handle_raw(
        json.dumps({"event": "subscribed", "channel": "book", "chanId": 9, "symbol": "fUST"})
    )
    client.handle_raw(json.dumps([9, [], 10]))

    assert snapshots == [("fUST", [], 10)]
