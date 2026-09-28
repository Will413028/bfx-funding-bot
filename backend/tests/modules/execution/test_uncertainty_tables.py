"""Schema constraints for submission attempts and execution uncertainties."""

from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)


def test_submission_attempt_unique_decision_and_open_scope_indexes_are_declared() -> None:
    """DDL, not callers, is the final backstop against duplicate writes."""
    assert SubmissionAttemptRow.__table__.c.execution_decision_id.unique is True
    uncertainty_table = ExecutionUncertaintyRow.__table__
    index_names = {index.name for index in uncertainty_table.indexes}
    assert "uq_execution_uncertainties_open_scope" in index_names
    attempt_fk_targets = {fk.target_fullname for fk in SubmissionAttemptRow.__table__.foreign_keys}
    assert "execution_decisions.decision_id" in attempt_fk_targets
    venue_fk = next(
        constraint
        for constraint in uncertainty_table.foreign_key_constraints
        if constraint.name == "fk_execution_uncertainties_venue_offer"
    )
    assert [column.name for column in venue_fk.columns] == [
        "exchange_account_id",
        "deployment_environment",
        "venue_offer_id",
    ]
