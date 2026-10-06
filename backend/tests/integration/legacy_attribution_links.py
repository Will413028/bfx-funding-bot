"""Migration ``a0b1c2d3e4f5``'s copy of the legacy attribution links, run again on demand.

A test that writes legacy rows after its database reached head (the legacy process of an e2e
run) replays the migration's own ``materialize`` -- the exact code that ran on the VM -- so the
weekly sees those rows the way production sees the frozen tables' copy.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REVISION = "a0b1c2d3e4f5"
PARENT = "f9a0b1c2d3e4"
_PATH = Path(__file__).resolve().parents[2] / "alembic/versions" / f"{REVISION}_attribution_legacy_links.py"


def migration() -> Any:
    spec = importlib.util.spec_from_file_location("attribution_legacy_links_migration", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def materialize(factory: async_sessionmaker[AsyncSession]) -> None:
    module = migration()
    async with factory.begin() as session:
        await session.run_sync(lambda s: module.materialize(s.connection()))
