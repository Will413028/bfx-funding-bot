from __future__ import annotations

import importlib.util
from pathlib import Path

from bfx_funding_bot.modules.execution.safety.tables import NavPeakRow
from bfx_funding_bot.modules.live_validation.tables import (
    AttributionWeeklyRow,
    ConfigRegimeRow,
)

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "9b2c3d4e5f6a_contract_exchange_account_identity.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("contract_identity_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_revision_is_linear_and_covers_all_account_scoped_tables() -> None:
    migration = _load_migration()

    assert migration.revision == "9b2c3d4e5f6a"
    assert migration.down_revision == "8a1b2c3d4e5f"
    assert set(migration._ACCOUNT_SCOPED_TABLES) == {
        "event_log",
        "offer_claims",
        "position_state",
        "reconcile_observation",
        "execution_decisions",
        "diagnostics",
        "nav_peak",
        "trading_halt",
        "attribution_weekly",
        "config_regime",
        "api_keys",
        "user_configs",
    }


def test_preflight_error_message_identifies_every_blocking_category() -> None:
    migration = _load_migration()

    message = migration._format_preflight_errors(
        null_rows={"event_log": 2},
        unmapped_realms={"event_log:legacy"},
        orphan_rows={"diagnostics": 1},
        nonzero_legacy_tables={"users": 3},
        identity_collisions={
            "offer_claims.primary_key": ("account/env/cid=one",),
        },
    )

    assert "event_log=2" in message
    assert "event_log:legacy" in message
    assert "diagnostics=1" in message
    assert "users=3" in message
    assert "offer_claims.primary_key" in message
    assert "account/env/cid=one" in message
    assert "contract migration blocked" in message


def test_contract_declares_uuid_rekey_collision_groups() -> None:
    migration = _load_migration()

    assert migration._IDENTITY_KEY_GROUPS["offer_claims.primary_key"] == (
        "offer_claims",
        ("exchange_account_id", "deployment_environment", "cid"),
        None,
    )
    assert migration._IDENTITY_KEY_GROUPS["config_regime.primary_key"] == (
        "config_regime",
        ("deployment_environment", "exchange_account_id", "recorded_at_ms"),
        None,
    )


def test_runtime_orm_primary_keys_match_post_cutover_uuid_contract() -> None:
    assert {column.name for column in NavPeakRow.__table__.primary_key} == {
        "exchange_account_id",
        "deployment_environment",
        "symbol",
    }
    assert {column.name for column in AttributionWeeklyRow.__table__.primary_key} == {
        "deployment_environment",
        "exchange_account_id",
        "cell",
        "week_start_ms",
    }
    assert {column.name for column in ConfigRegimeRow.__table__.primary_key} == {
        "deployment_environment",
        "exchange_account_id",
        "recorded_at_ms",
    }
