"""Rolling prefix hash: verifying an immutable prefix must cost one row, not a scan.

`canonical_event_hash` hashes a whole sequence, so binding a projection to its covered
prefix with it would re-read the prefix on every check — the O(history) cost this work
exists to remove. The rolling form carries the same evidence incrementally.
"""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.execution.event_store.canonical import (
    GENESIS_PREFIX_HASH,
    canonical_event_hash,
    rolling_prefix_hash,
    rolling_prefix_hashes,
)
from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows


def _seq(rows):
    for index, row in enumerate(rows, start=1):
        row.event_seq = index
    return rows


def test_chain_is_deterministic_and_prefix_scoped() -> None:
    """The hash after N events depends on exactly the first N events."""
    rows = _seq(historical_rows())
    chain = rolling_prefix_hashes(rows)

    assert len(chain) == len(rows)
    assert chain == rolling_prefix_hashes(rows), "same input must reproduce the same chain"
    # Extending the stream must not disturb any earlier prefix: that is what makes a
    # stored covered_event_hash checkable against a now-advancing stream.
    assert rolling_prefix_hashes(rows[:3]) == chain[:3]
    # And each link is exactly one step from its predecessor.
    assert chain[0] == rolling_prefix_hash(GENESIS_PREFIX_HASH, rows[0])
    assert chain[2] == rolling_prefix_hash(chain[1], rows[2])


def test_chain_detects_reorder_and_mutation() -> None:
    """A projection that lost, moved or altered a row must fail the prefix check."""
    rows = _seq(historical_rows())
    baseline = rolling_prefix_hashes(rows)[-1]

    swapped = _seq(historical_rows())
    swapped[1], swapped[2] = swapped[2], swapped[1]
    assert rolling_prefix_hashes(swapped)[-1] != baseline, "order must be part of the evidence"

    mutated = _seq(historical_rows())
    mutated[0].payload = {**mutated[0].payload, "amount": "999999"}
    assert rolling_prefix_hashes(mutated)[-1] != baseline, "content must be part of the evidence"

    dropped = _seq(historical_rows()[:-1])
    assert rolling_prefix_hashes(dropped)[-1] != baseline, "a missing row must not verify"


def test_chain_discriminates_wherever_the_sequence_hash_does() -> None:
    """Equivalent discriminating power to the existing contract, incrementally."""
    rows = _seq(historical_rows())
    other = _seq(historical_rows(first_amount="7"))

    assert canonical_event_hash(rows) != canonical_event_hash(other)
    assert rolling_prefix_hashes(rows)[-1] != rolling_prefix_hashes(other)[-1]

    same = _seq(historical_rows())
    assert canonical_event_hash(rows) == canonical_event_hash(same)
    assert rolling_prefix_hashes(rows)[-1] == rolling_prefix_hashes(same)[-1]


def test_missing_sequence_is_rejected_not_skipped() -> None:
    """An unsequenced row cannot silently contribute a hash."""
    rows = historical_rows()
    rows[0].event_seq = None
    with pytest.raises(ValueError):
        rolling_prefix_hashes(rows)
