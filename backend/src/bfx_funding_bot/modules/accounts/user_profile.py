from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class UserProfile(Base):
    """App-side profile keyed by the retired Better Auth user ID (TEXT).

    Carries SaaS fields (plan, future org_id). The ``auth`` schema it pointed
    at was dropped in 9d-7 (f836fdee9bf3); ``user_id`` is not rekeyed because
    the vault's AAD binds it.
    """

    __tablename__ = "user_profiles"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # A Better Auth TEXT id, NOT a uuid — store as Text. No foreign key: the
    # one to auth.user.id (fk_user_profiles_user) went with the auth schema
    # in f836fdee9bf3.
    user_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    org_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    # Migration f1a2b3c4d5e6 replaced the bare unique index with a UNIQUE
    # CONSTRAINT (PG requires a constraint, not an index, as an FK target).
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_profiles_user_id"),)
