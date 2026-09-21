from __future__ import annotations

import json
import zlib

from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel


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


# One real fUSD book state and the checksum the venue itself published for it,
# captured from the live public feed on 2026-09-21. Synthesising the expected
# value from the same code under test proves nothing; only the venue's own
# number can. Note what this book contains: integral amounts (-20000, 666,
# -2000000) and repeated rates at different periods.
_VENUE_CHECKSUM = 1208920331
_VENUE_BIDS: list[list[object]] = [
    [0.000273972602739726, 120, 1, -1778821.80687228],
    [0.0001712, 120, 1, -20000],
    [0.00017111, 120, 1, -2000000],
    [0.000161, 120, 1, -100000],
    [0.000161, 60, 1, -20000],
    [0.0001603, 120, 1, -20000],
    [0.00016, 120, 2, -10009162.66122322],
    [0.0001503, 90, 1, -20000],
    [0.00015, 29, 1, -149122.66199579],
    [0.00015, 7, 1, -6116.52506009],
    [0.00015, 2, 1, -545655.60273833],
    [0.00014252, 120, 1, -10770.76644364],
    [0.0001403, 90, 1, -20000],
    [0.00014001, 2, 1, -119774.0143323],
    [0.00013608512169813826, 2, 1, -4718.52507741],
    [0.00013608452816507954, 2, 1, -4718.52507741],
    [0.00013606067794775094, 2, 1, -4718.52507741],
    [0.00013605888992708018, 2, 1, -4718.52507741],
    [0.00013605803104953525, 2, 1, -4718.52507741],
    [0.0001360044456692742, 2, 1, -4718.52507741],
    [0.00013600402544270958, 2, 1, -4718.52507741],
    [0.0001360016980060091, 2, 1, -4718.52507741],
    [0.00013599603173869548, 2, 1, -4718.52507741],
    [0.00013599479817676577, 2, 1, -4718.52507741],
    [0.00013299999999999998, 20, 1, -4018.00283321],
]
_VENUE_ASKS: list[list[object]] = [
    [0.0002462, 2, 1, 1126.84],
    [0.00024697, 2, 1, 1006.4168694],
    [0.000247376, 2, 1, 1006.41686936],
    [0.00024777, 2, 3, 1347.46492957],
    [0.0002477789, 2, 1, 157.34467911],
    [0.000247782, 2, 1, 1006.41686936],
    [0.0002477889, 2, 2, 7157.52413766],
    [0.00024779, 3, 1, 472.73],
    [0.0002478, 2, 2, 12290.50094025],
    [0.0002478, 8, 2, 2956.935],
    [0.00024781, 2, 18, 96465.55405831],
    [0.0002478109589041096, 2, 2, 737359.53493014],
    [0.00024781369863013697, 2, 1, 2068.74172152],
    [0.0002478181165138993, 2, 2, 712.61432872],
    [0.00024782, 2, 400, 782293.96559889],
    [0.0002478338, 2, 1, 666],
    [0.00024784, 2, 121, 91440.069919],
    [0.00024789, 3, 1, 401.75],
    [0.00024792844556186704, 2, 1, 509.14506038],
    [0.00024794, 2, 6, 2900.16305675],
    [0.00024795998, 2, 1, 666],
    [0.00024796, 2, 4, 2000.26],
    [0.00024797, 7, 2, 302],
    [0.00024797, 2, 171, 398896.6798962],
    [0.00024798, 2, 3, 40423.4724354],
]


def test_checksum_matches_the_value_the_venue_published_for_this_book() -> None:
    client = FundingBookWSClient(symbols=("fUSD",))

    checksum = client.checksum(
        bids=[FundingBookLevel.from_bitfinex(row) for row in _VENUE_BIDS],
        asks=[FundingBookLevel.from_bitfinex(row) for row in _VENUE_ASKS],
    )

    assert checksum == _VENUE_CHECKSUM


def test_checksum_token_omits_the_period_and_keeps_whole_amounts_whole() -> None:
    """The two ways the old token diverged from the venue's, pinned separately.

    A parsed 3600 rendered as "3600.0", and the period was included at all --
    either one alone made every live checksum disagree.
    """
    client = FundingBookWSClient(symbols=("fUST",))

    checksum = client.checksum(
        bids=[FundingBookLevel(rate=0.0002, period=7, count=1, amount=-100.0)],
        asks=[FundingBookLevel(rate=0.00021, period=30, count=1, amount=3600.0)],
    )

    unsigned = zlib.crc32(b"0.0002:-100:0.00021:3600")
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
