"""Where the frozen legacy authority's tables live (S1-8 D4b, migration ``c2d3e4f5a6b7``).

The ledger has been the only capital authority since the switch. The twelve tables only the
legacy authority wrote (frozen by ``e8f9a0b1c2d3``) moved out of ``public`` into ``SCHEMA``:
no role writes them (``archive_frozen`` refuses the owner too), and only the web API's archived execution history reads
them at runtime (``bfx_webapi``, column-scoped ``event_log``). End state: dumped to offsite
and dropped once Will drops the pre-switch History.
"""

from __future__ import annotations

from typing import Final

SCHEMA: Final = "legacy_archive"
TABLES: Final = frozenset({
    "event_log",
    "event_prefix_hashes",
    "projection_heads",
    "position_state",
    "venue_offer_state",
    "venue_credit_state",
    "reconcile_observation",
    "offer_claims",
    "submission_attempts",
    "execution_uncertainties",
    "capital_snapshot_queries",
    "capital_snapshots",
})


def qualified(table: str) -> str:
    """``table`` with the schema it lives in: the archive for the twelve, else ``public``."""
    return f"{SCHEMA}.{table}" if table in TABLES else f"public.{table}"
