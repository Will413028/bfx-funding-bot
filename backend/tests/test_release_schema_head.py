"""The declared schema head must be the migration head it claims to run on.

`RELEASE_SCHEMA_HEAD` is compared at startup against the database's actual
alembic heads and against the release manifest, so a migration that lands
without moving it makes the daemon refuse to boot and makes the artifact
advertise a schema the build no longer runs on. Keeping it hand-maintained is
deliberate -- adopting a schema should be a decision, not a side effect of a
file appearing -- but forgetting to move it should fail here, in a second,
rather than after building and shipping a release.
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from bfx_funding_bot.modules.execution.event_store.writer import _READY_PROJECTOR_MIGRATIONS
from bfx_funding_bot.modules.execution.projection_cutover.archive import _ARCHIVE_READY_MIGRATIONS
from bfx_funding_bot.modules.execution.release_worker import RELEASE_SCHEMA_HEAD

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic_heads() -> tuple[str, ...]:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return tuple(ScriptDirectory.from_config(config).get_heads())


def test_release_schema_head_matches_the_alembic_head() -> None:
    heads = _alembic_heads()
    assert heads == (RELEASE_SCHEMA_HEAD,), (
        f"RELEASE_SCHEMA_HEAD is {RELEASE_SCHEMA_HEAD!r} but alembic's head is "
        f"{heads!r}. A migration landed without moving the declared head: update "
        f"RELEASE_SCHEMA_HEAD in release_worker.py to the new revision."
    )


def test_migrations_have_exactly_one_head() -> None:
    """Two heads mean a branched history, which the startup check cannot express."""
    heads = _alembic_heads()
    assert len(heads) == 1, f"expected a single migration head, found {heads!r}"


def test_head_is_classified_for_the_projector_cursor() -> None:
    """The head a deployment lands on must be judged against the cursor contract.

    `_assert_projector_migration_ready` fails closed when the database's current
    revision is missing from the allow-list, so a migration that lands without
    being classified stops the daemon at startup -- after a release has been
    built and shipped. The list is hand-maintained on purpose: whether a
    migration preserves the seeded cursor contract is a judgement, not something
    a test can infer. What the test can do is refuse to let it be skipped.
    """
    heads = _alembic_heads()
    unclassified = set(heads) - set(_READY_PROJECTOR_MIGRATIONS)
    assert not unclassified, (
        f"migration head {sorted(unclassified)} is not classified in "
        f"_READY_PROJECTOR_MIGRATIONS. Decide whether it preserves the seeded "
        f"cursor contract, then add it to the allow-list in writer.py."
    )


def test_head_is_classified_for_the_projection_archive() -> None:
    """A cutover capture refuses any head missing from the archive allow-list.

    Unclassified, the new head makes `capture_archive` raise "archive migration
    not ready", which only the PostgreSQL integration suite would notice. Decide
    whether the migration changes an archived table or event_log, then add it.
    """
    heads = _alembic_heads()
    unclassified = set(heads) - set(_ARCHIVE_READY_MIGRATIONS)
    assert not unclassified, (
        f"migration head {sorted(unclassified)} is not classified in "
        f"_ARCHIVE_READY_MIGRATIONS. Decide whether it changes an archived table "
        f"or event_log, then add it to the allow-list in projection_cutover/archive.py."
    )
