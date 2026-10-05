"""The scope tables are derived from the ledger table metadata; pin them to the audited lists."""

from __future__ import annotations

from bfx_funding_bot.modules.ledger.table_digest import _SCOPE_PARENT
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES, SCOPE_PARENT, SCOPE_ROOT_TABLES

ROOTS = (
    "capital_command_clock",
    "ledger_observation_query",
    "ledger_observation",
    "venue_offer_mirror",
    "venue_credit_mirror",
    "submission_attempt_journal",
    "quarantine_opening",
    "execution_resolution_journal",
    "accepted_capital_basis",
)
OBSERVATION = ("observation_id", "ledger_observation", "id")
BASIS = ("basis_id", "accepted_capital_basis", "id")
CHILDREN = {
    "ledger_observation_wallet": OBSERVATION,
    "ledger_observation_offer": OBSERVATION,
    "ledger_observation_credit": OBSERVATION,
    "ledger_observation_offer_history": OBSERVATION,
    "ledger_observation_credit_history": OBSERVATION,
    "ledger_observation_trade": OBSERVATION,
    "transport_outcome_journal": ("attempt_id", "submission_attempt_journal", "attempt_id"),
    "quarantine_member": ("quarantine_id", "quarantine_opening", "quarantine_id"),
    "accepted_capital_basis_symbol": BASIS,
    "accepted_capital_basis_cell": BASIS,
    "accepted_capital_basis_credit": BASIS,
    "accepted_capital_basis_credit_cell": BASIS,
    "accepted_capital_basis_attempt": BASIS,
    "accepted_capital_basis_quarantine": BASIS,
}


def test_the_derived_scope_roots_are_the_audited_list() -> None:
    assert tuple(table.name for table in SCOPE_ROOT_TABLES) == ROOTS


def test_the_derived_scope_parents_are_the_audited_mapping() -> None:
    assert SCOPE_PARENT == {**dict.fromkeys(ROOTS), **CHILDREN}
    assert set(SCOPE_PARENT) == {table.name for table in LEDGER_TABLES}
    assert _SCOPE_PARENT is SCOPE_PARENT
