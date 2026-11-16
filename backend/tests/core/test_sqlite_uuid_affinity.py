"""A PostgreSQL ``UUID`` column on the sqlite unit-test schema keeps every UUID a UUID.

Rendered as ``UUID`` it had NUMERIC affinity: a uuid4 whose hex reads as a number came back as
a float, and ``test_credential_rotation`` failed at random (CI run 43468867597).
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, select

from bfx_funding_bot.modules.accounts.rotation_tables import CredentialRotationRecipientRow

# The column type the failing read went through (``PG_UUID(as_uuid=True)``).
RECIPIENT_ID = CredentialRotationRecipientRow.__table__.c.recipient_id.type

# Hex that sqlite would read as a number: digits around one ``e``, and all digits.
NUMERIC_LOOKING = [UUID("12345678901234567890123456e12345"),
                   UUID("12345678901234567890123456789012")]


@pytest.mark.parametrize("value", NUMERIC_LOOKING, ids=str)
def test_a_numeric_looking_uuid_round_trips(value: UUID) -> None:
    table = Table("t", MetaData(), Column("id", RECIPIENT_ID, primary_key=True))
    engine = create_engine("sqlite://")
    try:
        table.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(table.insert().values(id=value))
            assert conn.exec_driver_sql("SELECT typeof(id) FROM t").scalar_one() == "text"
            assert conn.execute(select(table.c.id)).scalar_one() == value
    finally:
        engine.dispose()
