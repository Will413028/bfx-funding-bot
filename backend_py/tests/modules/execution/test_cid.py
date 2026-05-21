"""cid generator: deterministic, day-bounded, int63."""
from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from bfx_funding_bot.modules.execution.cid import (
    BITFINEX_CID_MAX,
    generate_cid,
)


def test_determinism_same_inputs_same_output() -> None:
    corr = UUID("12345678-1234-5678-1234-567812345678")
    d = date(2026, 5, 21)
    assert generate_cid(corr, d) == generate_cid(corr, d)


def test_day_boundary_changes_cid() -> None:
    corr = UUID("12345678-1234-5678-1234-567812345678")
    cid_y = generate_cid(corr, date(2026, 5, 21))
    cid_y_plus_1 = generate_cid(corr, date(2026, 5, 22))
    assert cid_y != cid_y_plus_1


def test_cid_is_positive_int63() -> None:
    corr = uuid4()
    d = date(2026, 5, 21)
    cid = generate_cid(corr, d)
    assert isinstance(cid, int)
    assert 0 < cid <= BITFINEX_CID_MAX
    assert BITFINEX_CID_MAX == 0x7FFF_FFFF_FFFF_FFFF


@given(
    corr=st.uuids(version=4),
    days_offset=st.integers(min_value=0, max_value=365),
)
@settings(max_examples=200, deadline=None)
def test_cid_always_in_range(corr: UUID, days_offset: int) -> None:
    d = date.fromordinal(date(2026, 1, 1).toordinal() + days_offset)
    cid = generate_cid(corr, d)
    assert 0 < cid <= BITFINEX_CID_MAX


def test_property_no_collision_10k_samples() -> None:
    import random
    rng = random.Random(0xBFB)
    seen: set[int] = set()
    collisions = 0
    for _ in range(10_000):
        corr = uuid4()
        d = date(2026, 1, 1).replace(month=rng.randint(1, 12), day=rng.randint(1, 28))
        cid = generate_cid(corr, d)
        if cid in seen:
            collisions += 1
        seen.add(cid)
    # 10k samples over 8-byte hash space: birthday-bound collision probability
    # is ~10^-12; observing any collision indicates a generator bug.
    assert collisions == 0, f"unexpected collisions: {collisions}"
