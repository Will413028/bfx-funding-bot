"""Every simulated venue must end a test without internal failures."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.modules.simulated_venue import helpers


@pytest.fixture(autouse=True)
def _no_internal_failures_at_teardown() -> Iterator[None]:
    helpers.WORLDS.clear()
    yield
    leaked = [(w.venue.internal_failures, w.venue.unexpected)
              for w in helpers.WORLDS if w.venue.internal_failures or w.venue.unexpected]
    helpers.WORLDS.clear()
    # Tests that provoke one on purpose assert it and then clear the list.
    assert not leaked, f"simulator-internal failures or unexpected requests: {leaked}"
