from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base


class UserProfile(Base):
    """App-side profile keyed to the Better Auth user (auth.user.id is TEXT).

    Carries SaaS fields (plan, future org_id) that Better Auth's user table
    does not own. JIT-provisioned on first authenticated request.
    """

    __tablename__ = "user_profiles"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # auth.user.id is a Better Auth TEXT id, NOT a uuid — store as Text.
    #
    # NO model-level ForeignKey here on purpose. The FK to auth.user.id is
    # enforced at the DB level by the migration ONLY (constraint
    # fk_user_profiles_user, ondelete=CASCADE). The auth.* schema is
    # Better-Auth-managed (TS) and is intentionally NOT modelled in this ORM's
    # Base.metadata. A model-level ForeignKey would pull auth.user into the
    # create_all() table-sort graph, and test fixtures that call
    # Base.metadata.create_all() without the auth schema would raise
    # NoReferencedTableError. DB constraint + integration FK assertion are
    # satisfied by the migration.
    user_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'free'"))
    org_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_user_profiles_user_id", "user_id", unique=True),)
