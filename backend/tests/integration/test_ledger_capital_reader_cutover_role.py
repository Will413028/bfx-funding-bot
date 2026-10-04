"""The ledger capital reader under the cutover reader's column grants.

The comparison runs the reader as ``bfx_cutover_reader`` (``SET LOCAL ROLE`` inside the
snapshot transaction). Every scenario below is the owner-run scenario of the file it comes
from, with each ``book.read`` executed under that role: a column the reader selects but the
role may not read fails with ``permission denied``, and a changed answer fails the scenario's
own assertions. Mutation: select ``AcceptedCapitalBasisSymbolRow.conservation`` out of
``capital_reader`` and the unexplained-lending scenario fails with permission denied;
select a whole ORM row (``evidence``) and the first read fails.
"""

import pytest

from .test_ledger_capital_reader import (  # noqa: F401 - fixtures and scenarios
    Book,
    book,
    test_loan_turning_into_split_credits_stays_in_its_cells_exposure,
    test_missing_policy_blocks_only_its_symbol,
    test_outcomes_release_or_hold_commitments,
    test_partial_fill_does_not_add_original_reservation,
    test_policy_faults_are_scoped_to_their_symbol,
    test_quarantine_opened_after_the_basis_blocks_its_symbol,
    test_quarantine_resolved_after_its_basis_blocks_until_a_new_basis,
    test_reader_token_tracks_query_and_current_clock_including_tail,
    test_reflected_commitment_is_not_charged_twice,
    test_resolved_unknown_waits_for_a_new_basis,
    test_scopes_are_isolated,
    test_symbol_block_and_scope_block,
    test_tail_unknown_resolution_releases_or_holds,
    test_trade_attributed_credit_is_its_cells_exposure,
    test_u_counts_once_in_total_and_in_no_cell,
    test_unknown_blocks_only_its_symbol,
)
from .test_ledger_conservation import (  # noqa: F401 - scenarios
    test_fills_without_lending_block_the_capital_read,
    test_unexplained_lending_blocks_the_capital_read_until_the_next_basis,
)
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reads_run_as_the_cutover_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Book.read

    async def read_as_reader(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        assert "role" not in kwargs
        return await original(self, *args, role="bfx_cutover_reader", **kwargs)

    monkeypatch.setattr(Book, "read", read_as_reader)
