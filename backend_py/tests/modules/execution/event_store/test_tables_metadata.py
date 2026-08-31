import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401  (register)
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
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
