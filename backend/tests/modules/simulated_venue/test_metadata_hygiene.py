"""The venue's unit tests never leave half of the schema registered (P1a hygiene finding).

A test module that imports ledger tables without the tables they reference makes the shared
``Base.metadata.create_all`` of the PostgreSQL fixtures fail in a subset run. The venue's unit tests
moved their ledger-backed cases to tests/integration and import no table module but the venue's
own, which has no foreign key.
"""
from __future__ import annotations

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.simulated_venue.tables import SimVenueEventRow


def test_every_registered_foreign_key_resolves() -> None:
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            assert foreign_key.column is not None, (table.name, foreign_key)


def test_the_venue_table_is_independent_of_the_bots_tables() -> None:
    assert SimVenueEventRow.__table__.foreign_keys == set()

