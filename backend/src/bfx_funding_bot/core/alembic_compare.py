"""Project-specific Alembic autogenerate comparison policy.

The application metadata intentionally omits a few Better Auth foreign keys
that are owned by hand-crafted migrations. PostgreSQL also renders a generic
JSON server default with a ``::json`` cast, while SQLAlchemy metadata keeps the
same default uncast. Compare those textual defaults after removing only the
trailing dialect type cast so a real default change still remains visible to
Alembic.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import JSON, Column

_JSON_TYPE_CAST = re.compile(r"::jsonb?\s*$", re.IGNORECASE)
_MIGRATION_MANAGED_FOREIGN_KEYS = frozenset(
    {
        ("public", "api_keys", "fk_api_keys_user_profile"),
        ("public", "user_configs", "fk_user_configs_user_profile"),
        ("public", "user_profiles", "fk_user_profiles_user"),
    }
)
_IDENTITY_CONTRACT_TABLES = frozenset(
    {
        "execution_decisions",
        "diagnostics",
        "nav_peak",
        "attribution_weekly",
        "config_regime",
        "api_keys",
        "user_configs",
    }
)
_LEGACY_REMOVED_TABLES = frozenset({"users", "executions", "billing_records"})
_RELEASE_ARCHIVE = "release_archive"


def _normalize_json_default(rendered_default: str) -> str:
    return _JSON_TYPE_CAST.sub("", rendered_default).strip()


def compare_server_default(
    context: Any,
    inspected_column: Column[Any],
    metadata_column: Column[Any],
    rendered_inspected_default: str | None,
    metadata_default: Any,
    rendered_metadata_default: str | None,
) -> bool | None:
    """Compare JSON defaults without PostgreSQL's unsupported ``json =`` SQL.

    ``None`` delegates non-JSON columns to Alembic's normal dialect
    comparison. For JSON columns, ``False`` means equivalent and ``True``
    means a real default difference.
    """

    del context, metadata_default
    if not isinstance(inspected_column.type, JSON) and not isinstance(
        metadata_column.type, JSON
    ):
        return None
    if rendered_inspected_default is None or rendered_metadata_default is None:
        return None
    return _normalize_json_default(rendered_inspected_default) != _normalize_json_default(
        rendered_metadata_default
    )


def include_object(
    object_: Any,
    name: str,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Keep the autogenerate surface aligned with migration ownership."""

    table = getattr(object_, "table", None)
    schema = getattr(table, "schema", None) or "public"
    table_name = getattr(table, "name", None)
    # The retired release ceremony's tables, frozen in their own schema by
    # migration c74d45a54e46: history, not application metadata.
    if (type_ == "table" and getattr(object_, "schema", None) == _RELEASE_ARCHIVE) or (
        schema == _RELEASE_ARCHIVE
    ):
        return False
    # ``exchange_account_id`` is nullable in ORM metadata solely so the
    # SQLite unit fixtures can continue to construct historical synthetic
    # realms.  PostgreSQL's forward-only Halt 1 contract migration owns the
    # NOT NULL/FK transition; do not report that intentional database-only
    # constraint as autogenerate drift.
    if (
        type_ == "column"
        and name == "exchange_account_id"
        and table_name in _IDENTITY_CONTRACT_TABLES
    ):
        # Ignore the database-only NOT NULL/FK contract once the column is
        # reflected, but keep metadata-only columns visible so an old database
        # still reports the required additive migration as drift.  Alembic
        # invokes this hook for both sides of a matched column; ``compare_to``
        # is non-None on the metadata side of a nullable-only difference.
        if compare_to is not None:
            return False
        return not reflected
    # Alembic cannot compare a generated column (its server-default comparison
    # reads ``.arg.text`` from the reflected ``Computed``). A matched pair is
    # skipped; a missing or extra generated column still shows as drift, and
    # test_generated_columns_match_the_model compares the skipped shape.
    if type_ == "column" and compare_to is not None and getattr(object_, "computed", None):
        return False
    # These scaffold models remain importable by the pre-cutover application
    # migration, but their public tables are deliberately dropped at the
    # contract boundary.  They are not part of the post-cutover metadata
    # surface and must not make ``alembic check`` suggest recreating them.
    if type_ == "table" and name in _LEGACY_REMOVED_TABLES:
        return False
    return not (
        (type_ == "table" and name == "atlas_schema_revisions")
        or (
            type_ == "foreign_key_constraint"
            and reflected
            and (
                (schema, table_name, name) in _MIGRATION_MANAGED_FOREIGN_KEYS
                or (
                    table_name in _IDENTITY_CONTRACT_TABLES
                    and name == f"fk_{table_name}_exchange_account"
                )
            )
        )
    )
