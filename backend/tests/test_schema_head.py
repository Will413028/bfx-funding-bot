"""The schema a build runs on, and the contracts that span migrations, are derived.

Nothing about a new migration needs to be remembered elsewhere: the build head
comes from ``alembic/`` itself, and each contract's revisions follow from the
``ledger_contract`` every migration declares. What a test can do is refuse a
migration that does not declare, and refuse two heads.
"""
import pytest

from bfx_funding_bot.core import schema_head
from bfx_funding_bot.core.schema_head import (
    ARCHIVE_CONTRACT_BASE,
    DECLARED_AFTER,
    PROJECTOR_CONTRACT_BASE,
    SchemaHeadMismatch,
    build_head,
    contract_revisions,
    declared_contract,
    migration_scripts,
)


def _after_declared() -> list[str]:
    return [s.revision for s in migration_scripts().iterate_revisions(build_head(), DECLARED_AFTER)
            if s.revision != DECLARED_AFTER]


def test_the_build_has_exactly_one_head() -> None:
    assert len(migration_scripts().get_heads()) == 1
    assert build_head() == migration_scripts().get_current_head()


def test_every_new_migration_declares_the_ledger_contract() -> None:
    """Preserves or changes the projector cursor / projection archive contract:
    a judgement the author makes, never a default."""
    undeclared = [r for r in _after_declared() if declared_contract(r) not in {"preserved", "changed"}]
    assert not undeclared, (
        f"migrations {undeclared} must set ledger_contract = \"preserved\" or \"changed\" "
        "(see core/schema_head.py)")


def test_the_contracts_hold_from_their_base_to_the_head() -> None:
    projector = contract_revisions(PROJECTOR_CONTRACT_BASE)
    archive = contract_revisions(ARCHIVE_CONTRACT_BASE)
    assert build_head() in projector and build_head() in archive
    assert {PROJECTOR_CONTRACT_BASE, ARCHIVE_CONTRACT_BASE} <= projector
    assert ARCHIVE_CONTRACT_BASE in archive and PROJECTOR_CONTRACT_BASE not in archive
    # Before the base the contract did not exist.
    base = migration_scripts().get_revision(PROJECTOR_CONTRACT_BASE)
    assert base.down_revision not in projector


def test_a_migration_that_changes_the_contract_starts_it_again(monkeypatch) -> None:
    changed = _after_declared()[len(_after_declared()) // 2]
    real = schema_head.declared_contract
    monkeypatch.setattr(schema_head, "declared_contract",
                        lambda r: "changed" if r == changed else real(r))
    schema_head.contract_revisions.cache_clear()
    try:
        ready = contract_revisions(PROJECTOR_CONTRACT_BASE)
        assert changed in ready and build_head() in ready
        assert migration_scripts().get_revision(changed).down_revision not in ready
    finally:
        schema_head.contract_revisions.cache_clear()


def test_two_heads_are_refused(monkeypatch) -> None:
    class Two:
        def get_heads(self):
            return ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]

    monkeypatch.setattr(schema_head, "migration_scripts", lambda: Two())
    with pytest.raises(SchemaHeadMismatch, match="schema_heads_ambiguous"):
        build_head()
