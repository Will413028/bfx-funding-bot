"""Pin the ledger facade: observation DTOs, acceptance port and read contracts are public."""

import inspect
from dataclasses import fields

import bfx_funding_bot.modules.ledger as ledger
from bfx_funding_bot.modules.ledger._internal import observation
from bfx_funding_bot.modules.ledger.wiring import (
    build_command_journal,
    build_ledger_conservation_reader,
    build_ledger_journal,
    build_ledger_managed_offers,
    build_ledger_observations,
    build_ledger_uncertainties,
)

_PROMOTED = {
    "Wallet",
    "Offer",
    "Credit",
    "OfferHistory",
    "CreditHistory",
    "Trade",
    "Coverage",
    "Observation",
    "ObservationWindow",
    "Acceptance",
    "AcceptanceDecision",
    "CreditKind",
    "OfferStatus",
    "CreditStatus",
    "OfferTerminalKind",
    "CreditTerminalKind",
    "OFFER_STATUSES",
    "CREDIT_STATUSES",
    "OFFER_TERMINAL_KINDS",
    "CREDIT_TERMINAL_KINDS",
    "LedgerObservations",
}
_READS = {
    "CommandJournal",
    "CommandAttempt",
    "CommandOutcome",
    "CommandRefused",
    "CancelAdmitted",
    "LedgerUncertainties",
    "LedgerManagedOffers",
    "OpenUncertainty",
    "ManagedOffer",
    "ManagedOffers",
    "CancelProvenance",
    "ProvenanceConflict",
    "LedgerReadUnbounded",
    "Authorized",
    "AuthorizeRefused",
    "LedgerCapitalRead",
    "encode_basis_token",
    "parse_basis_token",
}


def _methods(protocol: type) -> set[str]:
    return {
        name
        for name, value in vars(protocol).items()
        if inspect.iscoroutinefunction(value) and not name.startswith("_")
    }


def test_promoted_names_are_exported() -> None:
    missing = (_PROMOTED | _READS) - set(ledger.__all__)
    assert not missing
    for name in _PROMOTED | _READS:
        assert getattr(ledger, name) is not None


def test_internal_acceptance_uses_the_facade_types() -> None:
    """One definition: the implementation validates and returns the public DTOs."""
    for name in ("Wallet", "Observation", "OfferHistory", "CreditHistory", "Acceptance"):
        assert getattr(observation, name) is getattr(ledger, name)
    assert observation.OFFER_STATUSES is ledger.OFFER_STATUSES
    assert frozenset(("active", "partially_filled")) == ledger.OFFER_STATUSES
    assert frozenset(("active",)) == ledger.CREDIT_STATUSES
    assert frozenset(("executed", "canceled")) == ledger.OFFER_TERMINAL_KINDS
    assert frozenset(("closed",)) == ledger.CREDIT_TERMINAL_KINDS


def test_ports_expose_their_methods() -> None:
    assert _methods(ledger.CommandJournal) == {"authorize", "record_outcome", "read_back_outcome", "admit_cancel"}
    assert "cid" not in {field.name for field in fields(ledger.CommandAttempt)}
    assert _methods(ledger.LedgerJournal) == {
        "bump_clock",
        "begin_query",
        "authorize_attempt",
        "close_dangling",
        "record_outcome",
        "read_back_outcome",
        "record_resolution",
        "open_quarantine",
        "add_quarantine_member",
    }
    assert not hasattr(build_ledger_journal(), "record_attempt")
    assert _methods(ledger.LedgerObservations) == {"begin_query", "accept", "observation_window"}
    assert _methods(ledger.LedgerUncertainties) == {"open_uncertainties"}
    assert _methods(ledger.LedgerManagedOffers) == {
        "managed_live_offers",
        "cancel_provenance",
        "fingerprints_in_use",
    }
    assert _methods(ledger.LedgerConservationReader) == {"latest"}
    for port, protocol in (
        (build_command_journal(None, max_snapshot_age_ms=1000), ledger.CommandJournal),
        (build_ledger_conservation_reader(), ledger.LedgerConservationReader),
        (build_ledger_journal(), ledger.LedgerJournal),
        (build_ledger_observations(), ledger.LedgerObservations),
        (build_ledger_uncertainties(), ledger.LedgerUncertainties),
        (build_ledger_managed_offers(), ledger.LedgerManagedOffers),
    ):
        for name in _methods(protocol):
            implementation = getattr(port, name)
            assert inspect.iscoroutinefunction(implementation)
            assert (
                list(inspect.signature(implementation).parameters)
                == list(inspect.signature(getattr(protocol, name)).parameters)[1:]
            )


def test_accept_signature_matches_the_implementation() -> None:
    port = inspect.signature(build_ledger_observations().accept)
    implementation = inspect.signature(observation.accept_observation)
    assert list(port.parameters) == list(implementation.parameters)


def test_journal_signatures_match_the_implementation() -> None:
    from bfx_funding_bot.modules.ledger._internal import journal

    for name in ("authorize_attempt", "close_dangling"):
        port = inspect.signature(getattr(build_ledger_journal(), name))
        implementation = inspect.signature(getattr(journal, name))
        assert port == implementation
    assert (
        inspect.signature(ledger.LedgerJournal.authorize_attempt).parameters["now_ms"].kind
        == inspect.Parameter.KEYWORD_ONLY
    )
