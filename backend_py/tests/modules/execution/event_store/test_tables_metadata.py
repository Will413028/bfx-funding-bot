import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401  (register)
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState


def test_event_store_tables_registered() -> None:
    tables = Base.metadata.tables
    assert "event_log" in tables
    assert "offer_claims" in tables
    assert "position_state" in tables
    assert "reconcile_observation" in tables


def test_event_log_columns() -> None:
    cols = Base.metadata.tables["event_log"].columns
    for name in ("event_seq", "account_id", "deployment_environment",
                 "event_type", "cid", "venue_offer_id", "venue_seq",
                 "payload", "occurred_at_ms", "recorded_at"):
        assert name in cols, name
    assert cols["event_seq"].primary_key is True


def test_event_log_v3_identity_constraint() -> None:
    table = EventLogRow.__table__
    cols = table.columns
    assert cols["event_id"].nullable is True
    assert cols["schema_version"].nullable is False
    assert any(
        constraint.name == "ck_event_log_v3_event_id"
        for constraint in table.constraints
    )
    identity_index = next(
        index for index in table.indexes if index.name == "uq_event_log_event_id"
    )
    assert identity_index.unique is True
    assert [column.name for column in identity_index.columns] == [
        "exchange_account_id",
        "deployment_environment",
        "event_id",
    ]


def test_position_state_exposes_v3_exposure_buckets() -> None:
    assert {
        "offered_amount",
        "lent_amount",
        "available_amount",
        "uncertain_amount",
        "last_venue_snapshot_at",
    } <= set(PositionStateRow.__table__.columns.keys())


def test_entity_tables_have_account_scoped_identity() -> None:
    assert [c.name for c in VenueOfferStateRow.__table__.primary_key.columns] == [
        "exchange_account_id",
        "deployment_environment",
        "venue_offer_id",
    ]
    assert [c.name for c in VenueCreditStateRow.__table__.primary_key.columns] == [
        "exchange_account_id",
        "deployment_environment",
        "credit_id",
    ]


def test_position_state_pk_is_uuid_env_symbol() -> None:
    pk = {c.name for c in Base.metadata.tables["position_state"].primary_key.columns}
    assert pk == {"exchange_account_id", "deployment_environment", "symbol"}


def test_registry_state_has_failed() -> None:
    assert RegistryState("failed") is RegistryState.FAILED


def test_offer_claims_composite_pk() -> None:
    pk_cols = [c.name for c in OfferClaimRow.__table__.primary_key.columns]
    assert pk_cols == ["exchange_account_id", "deployment_environment", "cid"]


def test_offer_claims_persists_audited_execution_decision_id() -> None:
    assert "execution_decision_id" in OfferClaimRow.__table__.columns
