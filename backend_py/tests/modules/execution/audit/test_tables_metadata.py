from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow


def test_execution_decision_table_is_registered_with_audit_columns() -> None:
    """Dropping audit evidence from the ORM table must break this schema contract."""
    table = Base.metadata.tables["execution_decisions"]
    expected_columns = {
        "decision_id",
        "account_id",
        "deployment_environment",
        "reconcile_id",
        "cell_id",
        "symbol",
        "signal_correlation_id",
        "outcome",
        "reason_code",
        "failed_dependency",
        "signal_rate",
        "applied_rate",
        "amount_usdt",
        "duration_days",
        "snapshot_id",
        "snapshot_hash",
        "snapshot_captured_at_ms",
        "snapshot_source",
        "snapshot_age_ms",
        "model_version",
        "model_hash",
        "model_evidence",
        "safety_result",
        "execution_policy",
        "service_version",
        "config_hash",
        "occurred_at_ms",
        "recorded_at_ms",
    }

    assert set(table.columns.keys()) == expected_columns
    assert [column.name for column in table.primary_key.columns] == ["decision_id"]
    assert ExecutionDecisionRow.__tablename__ == "execution_decisions"


def test_execution_decision_indexes_are_bounded_audit_queries() -> None:
    """Removing a bounded audit lookup index must break this metadata contract."""
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in ExecutionDecisionRow.__table__.indexes
    }

    assert indexes == {
        "idx_execution_decisions_env_occurred": ("deployment_environment", "occurred_at_ms"),
        "idx_execution_decisions_outcome_reason": ("outcome", "reason_code"),
        "idx_execution_decisions_symbol_occurred": ("symbol", "occurred_at_ms"),
    }
