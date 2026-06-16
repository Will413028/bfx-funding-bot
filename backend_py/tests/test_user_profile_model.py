"""#6 model-vs-DB drift guard. Migration f1a2b3c4d5e6 replaced the bare unique
index idx_user_profiles_user_id with a UNIQUE CONSTRAINT uq_user_profiles_user_id
(PG FK target requires a constraint). The mapped model must agree."""
from sqlalchemy import UniqueConstraint

from bfx_funding_bot.modules.accounts.user_profile import UserProfile


def test_user_profiles_has_unique_constraint_not_bare_index():
    table = UserProfile.__table__

    constraint_names = {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_user_profiles_user_id" in constraint_names

    # The unique constraint covers exactly user_id.
    uq = next(
        c for c in table.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_user_profiles_user_id"
    )
    assert [col.name for col in uq.columns] == ["user_id"]

    # The replaced bare unique index must be gone.
    index_names = {ix.name for ix in table.indexes}
    assert "idx_user_profiles_user_id" not in index_names
