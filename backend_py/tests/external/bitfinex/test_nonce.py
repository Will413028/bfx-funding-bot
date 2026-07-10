"""Shared monotonic µs nonce source (auth WS reconnect-flap fix)."""
from bfx_funding_bot.external.bitfinex.nonce import make_monotonic_us_nonce

_US_SCALE_FLOOR = 1_000_000_000_000_000  # 16 digits ≈ µs since epoch (year 2001+)


def test_strictly_increasing_even_faster_than_clock_tick():
    n = make_monotonic_us_nonce()
    vals = [n() for _ in range(5000)]
    # tight loop outruns the 1µs wall-clock resolution → the `last + 1` term must
    # still make every value distinct and increasing, or Bitfinex rejects "nonce:
    # small(equal)".
    assert vals == sorted(vals)
    assert len(set(vals)) == len(vals)


def test_us_scale_not_ms():
    # must standardize UP to µs: a ms-scale nonce on a key that already emitted µs
    # values would be permanently rejected.
    n = make_monotonic_us_nonce()
    assert n() >= _US_SCALE_FLOOR


def test_sources_are_independent_closures():
    a, b = make_monotonic_us_nonce(), make_monotonic_us_nonce()
    a()  # advance a only
    assert b() >= _US_SCALE_FLOOR  # b unaffected by a's state
