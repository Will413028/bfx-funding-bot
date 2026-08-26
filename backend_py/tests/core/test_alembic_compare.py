from sqlalchemy import (
    JSON,
    Column,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    text,
)

from bfx_funding_bot.core.alembic_compare import compare_server_default, include_object


def _column(column_type, default: str | None) -> Column:
    return Column(
        "value",
        column_type,
        server_default=text(default) if default is not None else None,
    )


def test_json_server_default_ignores_postgresql_type_cast() -> None:
    inspected = _column(JSON(), "'{}'::json")
    metadata = _column(JSON(), "'{}'")

    assert (
        compare_server_default(
            None,
            inspected,
            metadata,
            "'{}'::json",
            metadata.server_default,
            "'{}'",
        )
        is False
    )


def test_json_server_default_mismatch_is_reported() -> None:
    inspected = _column(JSON(), "'{}'::json")
    metadata = _column(JSON(), "'[]'")

    assert (
        compare_server_default(
            None,
            inspected,
            metadata,
            "'{}'::json",
            metadata.server_default,
            "'[]'",
        )
        is True
    )


def test_json_literal_content_is_not_treated_as_type_cast() -> None:
    inspected = _column(JSON(), "'\"rate::json\"'::json")
    metadata = _column(JSON(), "'\"rate\"'")

    assert (
        compare_server_default(
            None,
            inspected,
            metadata,
            "'\"rate::json\"'::json",
            metadata.server_default,
            "'\"rate\"'",
        )
        is True
    )


def test_non_json_server_default_uses_alembic_default_comparator() -> None:
    inspected = _column(Integer(), "0")
    metadata = _column(Integer(), "1")

    assert (
        compare_server_default(
            None,
            inspected,
            metadata,
            "0",
            metadata.server_default,
            "1",
        )
        is None
    )


def _foreign_key(table_name: str, schema: str | None, name: str):
    metadata = MetaData()
    table = Table(
        table_name,
        metadata,
        Column("user_id", Integer),
        ForeignKeyConstraint(["user_id"], ["user_profiles.user_id"], name=name),
        schema=schema,
    )
    return next(iter(table.foreign_key_constraints))


def test_include_object_filters_legacy_and_migration_managed_objects() -> None:
    assert not include_object(object(), "atlas_schema_revisions", "table", True, None)
    managed_fk = _foreign_key(
        "api_keys", "public", "fk_api_keys_user_profile"
    )
    assert not include_object(
        managed_fk, managed_fk.name, "foreign_key_constraint", True, None
    )
    assert not include_object(
        _foreign_key("user_configs", None, "fk_user_configs_user_profile"),
        "fk_user_configs_user_profile",
        "foreign_key_constraint",
        True,
        None,
    )
    assert not include_object(
        _foreign_key("user_profiles", "public", "fk_user_profiles_user"),
        "fk_user_profiles_user",
        "foreign_key_constraint",
        True,
        None,
    )


def test_include_object_keeps_unmanaged_or_metadata_objects() -> None:
    metadata = MetaData()
    table = Table("orders", metadata, Column("id", Integer, primary_key=True))

    assert include_object(table, "orders", "table", False, table)
    same_name_other_table = _foreign_key(
        "other_api_keys", "public", "fk_api_keys_user_profile"
    )
    assert include_object(
        same_name_other_table,
        same_name_other_table.name,
        "foreign_key_constraint",
        True,
        None,
    )
    same_name_other_schema = _foreign_key(
        "api_keys", "other", "fk_api_keys_user_profile"
    )
    assert include_object(
        same_name_other_schema,
        same_name_other_schema.name,
        "foreign_key_constraint",
        True,
        None,
    )
    assert include_object(
        _foreign_key("api_keys", "public", "fk_api_keys_user_profile"),
        "fk_api_keys_user_profile",
        "foreign_key_constraint",
        False,
        None,
    )
