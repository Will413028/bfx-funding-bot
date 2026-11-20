"""Column-type rendering for the sqlite unit-test schemas (tests/conftest.py imports it).

Production schemas are PostgreSQL, built by Alembic; sqlite exists only in unit tests, so this
override lives here and not in ``bfx_funding_bot.core.db``.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles


@compiles(PG_UUID, "sqlite")
def sqlite_uuid(element: PG_UUID[Any], compiler: Any, **kw: Any) -> str:
    """The PostgreSQL ``UUID`` column on sqlite as ``CHAR(32)``.

    Left alone it renders as ``UUID``, a type name sqlite gives NUMERIC affinity, so a stored
    hex that reads as a number (``...123e456...`` with only digits around the ``e``) came back
    as a float and broke the UUID result processor at random (CI run 43468867597). ``CHAR``
    gives TEXT affinity, as ``sqlalchemy.Uuid`` already renders it.
    """
    return "CHAR(32)"
