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
