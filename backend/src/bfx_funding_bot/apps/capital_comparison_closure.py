"""Closure verifier: the seeded ledger is the legacy closure at the capture point (pre-flight §C).

Runs in the comparison's one REPEATABLE READ READ ONLY transaction under the cutover reader role
(column grants: ``alembic/versions/d7e8f9a0b1c2_*``), per listed (account, environment), against
the seed command's evidence JSONL (``apps/ledger_seed.py``). The capture point is the seed's: the
final legacy accepted snapshot (``watermarks.snapshot_event_seq``), the seed observation and basis.
Every check compares both directions and reports each unmatched element as a violation with its
evidence; any violation makes the run exit non-zero.

``seed_anchor``
    evidence exists for exactly the listed scopes; the scope's one ``legacy_seed`` observation and
    its basis are the evidence's; the legacy final snapshot is still the latest and the legacy
    stream has not moved since the seed (``legacy_final_event_seq``).
``live_offers`` (1)
    legacy live managed offers (final classification ``offers``) <-> seeded attempts with an ack
    outcome (or a bound resolution) naming them, classified ``reflected``; legacy managed and
    foreign offers <-> the seed observation's live offers; and the live mirror: at the capture
    point (the scope's latest ledger query is the seed's) the mirror's live rows of the seed
    observation, after it (the runner has observed since) a mirror row for each.
``attempts`` (2)
    legacy attempts the final snapshot did not settle or reflect <-> seeded ``unresolved`` attempts
    with the same outcome kind; settled and reflected ones <-> seeded ones of the same class.
``open_uncertainties`` (3)
    legacy open uncertainties (subject: the attempt of an UNKNOWN, else the uncertainty id, F8)
    <-> the ledger's open uncertainties.
``credit_groups`` / ``symbols`` (4)
    legacy ``credit_cells`` groups <-> seed basis credits (symbol, amount, period, opening,
    attribution) with exact cell sets; legacy symbol totals and cells <-> seed basis symbol and
    cell rows.
``seed_provenance`` (5)
    every seeded attempt's ``seed_provenance`` resolves to its legacy attempt or claim, and every
    seeded quarantine to its legacy uncertainty; no attempt outside the seed basis carries one.
``fingerprints`` (6)
    per symbol, legacy ``fingerprints_in_use`` vs ``fingerprint_of`` of the ledger's; each strict
    difference is classified (legacy only: an open claim whose offer is not in the ledger's live
    book, which only the WS path releases -- at the capture point the seed's book, after it the
    runner's; ledger only: an acknowledged attempt no basis reflected) or a violation.
``trading_state`` (7)
    the scope's latest ``trading_state`` row is the evidence's watermark.
``requests`` (8)
    no pending uncertainty resolution request; every id the seed failed is ``failed
    superseded_by_authority_switch``, and no other carries that reason; the carried policy and
    trading-control requests are exactly the pending ones.
``projection_symbols`` (S1-4a R1-5)
    every symbol of a non-terminal legacy ``venue_offer_state`` / ``venue_credit_state`` row has a
    configured cell.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.apps.capital_comparison_ledger import (
    CreditGroup,
    basis_groups,
    legacy_credit_key,
)
from bfx_funding_bot.modules.execution.amount_fingerprint import fingerprints_in_use
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyRequestRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    VenueCreditStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.safety.tables import (
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    LedgerManagedOffers,
    LedgerUncertainties,
    Scope,
)
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisCellRow,
    AcceptedCapitalBasisQuarantineRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    ExecutionResolutionJournalRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
    VenueOfferMirrorRow,
)
from bfx_funding_bot.modules.trading import CapitalScope, fingerprint_of

CHECKS: Final = (
    "seed_anchor",
    "live_offers",
    "attempts",
    "open_uncertainties",
    "credit_groups",
    "symbols",
    "seed_provenance",
    "fingerprints",
    "trading_state",
    "requests",
    "projection_symbols",
)
CARRIED_TABLES: Final = ("capital_policy_requests", "trading_control_requests")
# The verifier's own statement of the seed contract (an independent path: nothing deployed may
# import the seed, ``test_ledger_seed_dormant``; tests pin these equal to the seed's).
SUPERSEDED_REASON: Final = "superseded_by_authority_switch"  # ruling 2026-10-02
LEDGER_OUTCOME_OF_LEGACY: Final = {
    "acknowledged": "ack", "rejected": "rejected", "not_sent": "not_sent", "unknown": "unknown",
}
LEDGER_ATTRIBUTION_OF_LEGACY: Final = {  # F7: recent_fill stays recent_fill (multi-cell)
    "funding_trade": "trade", "funding_trade_partial": "trade", "carried": "carry",
    "recent_fill": "recent_fill", "none": "unattributed",
}
_OPEN_CLAIMS: Final = (RegistryState.PENDING.value, RegistryState.CLAIMED.value)
_ZERO: Final = Decimal(0)

type Key = tuple[UUID, str]


class EvidenceRejectedError(ValueError):
    """The seed evidence file is not a committed, verified seed; ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SeedEvidence:
    """One scope's line of the seed command's evidence JSONL."""

    account_id: UUID
    environment: str
    observation_id: UUID
    basis_id: UUID
    legacy_final_event_seq: int
    snapshot_event_seq: int
    snapshot_query_id: UUID
    trading_state_max_id: int | None
    failed_uncertainty_requests: frozenset[UUID]
    carried_requests: Mapping[str, frozenset[UUID]]


@dataclass(frozen=True, slots=True)
class ClosureCheck:
    check: str
    account_id: UUID
    environment: str
    violations: tuple[Mapping[str, object], ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "violation" if self.violations else "ok"


def _int(value: object) -> int:
    if type(value) is not int:
        raise EvidenceRejectedError("seed_evidence_invalid")
    return value


def _ids(value: object) -> frozenset[UUID]:
    if not isinstance(value, list):
        raise EvidenceRejectedError("seed_evidence_invalid")
    return frozenset(UUID(str(item)) for item in value)


def parse_seed_evidence(lines: Iterable[str]) -> dict[Key, SeedEvidence]:
    """The committed seed's evidence per scope; refuses an uncommitted or unverified seed."""
    try:
        rows = [json.loads(line) for line in lines if line.strip()]
        summaries = [row for row in rows if row.get("kind") == "summary"]
        if len(summaries) != 1 or summaries[0].get("committed") is not True or (
            summaries[0].get("exit_code") != 0
        ):
            raise EvidenceRejectedError("seed_not_committed")
        verified: set[Key] = set()
        for row in rows:
            if row.get("kind") == "verification":
                scope = row["scope"]
                if row.get("mismatches") != []:
                    raise EvidenceRejectedError("seed_not_verified")
                verified.add((UUID(scope["account_id"]), str(scope["environment"])))
        found: dict[Key, SeedEvidence] = {}
        for row in rows:
            if row.get("kind") != "seed":
                continue
            scope, marks = row["scope"], row["watermarks"]
            key = (UUID(scope["account_id"]), str(scope["environment"]))
            if key in found:
                raise EvidenceRejectedError("seed_evidence_duplicate_scope")
            carried = row.get("carried_requests")
            if not isinstance(carried, dict) or set(carried) != set(CARRIED_TABLES):
                raise EvidenceRejectedError("seed_evidence_invalid")
            trading_state = marks["trading_state_max_id"]
            found[key] = SeedEvidence(
                key[0], key[1], UUID(row["observation_id"]), UUID(row["basis_id"]),
                _int(marks["legacy_final_event_seq"]), _int(marks["snapshot_event_seq"]),
                UUID(marks["snapshot_query_id"]),
                None if trading_state is None else _int(trading_state),
                _ids(row["failed_uncertainty_requests"]),
                {name: _ids(carried[name]) for name in CARRIED_TABLES},
            )
    except EvidenceRejectedError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise EvidenceRejectedError("seed_evidence_invalid") from None
    if not found or set(found) != verified:
        raise EvidenceRejectedError("seed_not_verified")
    return found


def _v(reason: str, **evidence: object) -> dict[str, object]:
    return {"reason": reason, **{key: _plain(value) for key, value in sorted(evidence.items())}}


def _plain(value: object) -> object:
    if isinstance(value, (set, frozenset, tuple, list)):
        return sorted(_plain(item) for item in value)  # type: ignore[type-var]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    return value


def _both(reason_left: str, reason_right: str, left: set[Any], right: set[Any], **evidence: object) -> list[dict[str, object]]:
    """Violations for each element only on one side (both directions)."""
    return [_v(reason_left, item=item, **evidence) for item in sorted(left - right, key=str)] + [
        _v(reason_right, item=item, **evidence) for item in sorted(right - left, key=str)
    ]


def _amount(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


@dataclass(frozen=True, slots=True)
class _Anchor:
    evidence: SeedEvidence
    classification: Mapping[str, Any]
    seed_query_id: UUID
    capture_point: bool


@dataclass(frozen=True, slots=True)
class _Seeded:
    """Seeded attempts (named columns), their basis classes, outcomes and bound offers."""

    attempts: dict[UUID, Any]
    classes: dict[UUID, str]
    outcomes: dict[UUID, tuple[str, str | None]]
    bound: dict[UUID, str]

    def offer_owner(self, attempt_id: UUID) -> str | None:
        kind, venue = self.outcomes.get(attempt_id, ("", None))
        return venue if kind == "ack" and venue else self.bound.get(attempt_id)


def _scoped(row: Any, key: Key) -> tuple[Any, Any]:
    return row.exchange_account_id == key[0], row.deployment_environment == key[1]


async def _anchor(session: AsyncSession, key: Key, evidence: SeedEvidence | None) -> tuple[_Anchor | None, list[dict[str, object]]]:
    if evidence is None:
        return None, [_v("seed_evidence_missing")]
    violations: list[dict[str, object]] = []
    observations = (await session.execute(select(
        LedgerObservationRow.id, LedgerObservationRow.query_id,
    ).where(*_scoped(LedgerObservationRow, key), LedgerObservationRow.origin == "legacy_seed"))).all()
    if [row.id for row in observations] != [evidence.observation_id]:
        violations.append(_v("seed_observation_mismatch", found=[row.id for row in observations],
                             expected=evidence.observation_id))
    bases = list(await session.scalars(select(AcceptedCapitalBasisRow.id).where(
        AcceptedCapitalBasisRow.observation_id == evidence.observation_id)))
    if bases != [evidence.basis_id]:
        violations.append(_v("seed_basis_mismatch", found=bases, expected=evidence.basis_id))
    latest = (await session.execute(select(
        CapitalSnapshotRow.event_seq, CapitalSnapshotRow.query_id, CapitalSnapshotRow.classification,
    ).where(*_scoped(CapitalSnapshotRow, key)).order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))).first()
    if latest is None or (latest.event_seq, latest.query_id) != (
        evidence.snapshot_event_seq, evidence.snapshot_query_id,
    ):
        violations.append(_v("legacy_final_snapshot_moved",
                             found=None if latest is None else latest.event_seq,
                             expected=evidence.snapshot_event_seq))
    head = int(await session.scalar(select(func.max(EventLogRow.event_seq)).where(
        *_scoped(EventLogRow, key))) or 0)
    if head != evidence.legacy_final_event_seq:
        violations.append(_v("legacy_stream_moved", found=head, expected=evidence.legacy_final_event_seq))
    latest_query = await session.scalar(select(LedgerObservationQueryRow.query_id).where(
        *_scoped(LedgerObservationQueryRow, key),
    ).order_by(LedgerObservationQueryRow.query_revision.desc()).limit(1))
    if violations or latest is None or len(observations) != 1:
        return None, violations
    seed_query = observations[0].query_id
    return _Anchor(evidence, latest.classification, seed_query, latest_query == seed_query), []


async def _seeded(session: AsyncSession, basis_id: UUID) -> _Seeded:
    rows = (await session.execute(select(
        SubmissionAttemptJournalRow.attempt_id, SubmissionAttemptJournalRow.execution_decision_id,
        SubmissionAttemptJournalRow.symbol, SubmissionAttemptJournalRow.basis_id,
        SubmissionAttemptJournalRow.seed_provenance, SubmissionAttemptJournalRow.intended_amount,
    ).where(SubmissionAttemptJournalRow.basis_id == basis_id))).all()
    attempts = {row.attempt_id: row for row in rows}
    classes = dict((await session.execute(select(
        AcceptedCapitalBasisAttemptRow.attempt_id, AcceptedCapitalBasisAttemptRow.classification,
    ).where(AcceptedCapitalBasisAttemptRow.basis_id == basis_id))).tuples().all())
    ids = sorted(attempts.keys() | classes.keys())
    outcomes: dict[UUID, tuple[str, str | None]] = {}
    bound: dict[UUID, str] = {}
    if ids:
        for attempt_id, kind, venue in await session.execute(select(
            TransportOutcomeJournalRow.attempt_id, TransportOutcomeJournalRow.kind,
            TransportOutcomeJournalRow.venue_offer_id,
        ).where(TransportOutcomeJournalRow.attempt_id.in_(ids))):
            outcomes[attempt_id] = (kind, venue)
        for attempt_id, venue in await session.execute(select(
            ExecutionResolutionJournalRow.attempt_id, ExecutionResolutionJournalRow.venue_offer_id,
        ).where(ExecutionResolutionJournalRow.attempt_id.in_(ids),
                ExecutionResolutionJournalRow.action == "bound_to_venue")):
            if venue:
                bound[attempt_id] = venue
    return _Seeded(attempts, classes, outcomes, bound)


async def _live_offers(session: AsyncSession, key: Key, anchor: _Anchor, seeded: _Seeded) -> ClosureCheck:
    ours = set((anchor.classification.get("offers") or {}).keys())
    foreign = set((anchor.classification.get("foreign") or {}).keys())
    owners: dict[str, set[UUID]] = {}
    for attempt_id in seeded.attempts:
        venue = seeded.offer_owner(attempt_id)
        if venue is not None:
            owners.setdefault(venue, set()).add(attempt_id)
    seed_live = set(await session.scalars(select(LedgerObservationOfferRow.venue_offer_id).where(
        LedgerObservationOfferRow.observation_id == anchor.evidence.observation_id)))
    violations: list[dict[str, object]] = []
    for venue in sorted(ours):
        named = owners.get(venue, set())
        if len(named) != 1:
            violations.append(_v("legacy_offer_without_one_attempt", venue_offer_id=venue, attempts=named))
        elif seeded.classes.get(next(iter(named))) != "reflected":
            violations.append(_v("legacy_offer_attempt_not_reflected", venue_offer_id=venue))
    for venue in sorted(seed_live - ours):
        reflected = {a for a in owners.get(venue, set()) if seeded.classes.get(a) == "reflected"}
        if reflected:
            violations.append(_v("reflected_attempt_offer_not_legacy_managed", venue_offer_id=venue,
                                 attempts=reflected))
    violations += _both("legacy_offer_not_seeded_live", "seeded_live_offer_not_legacy",
                        ours | foreign, seed_live)
    mirror = (await session.execute(select(
        VenueOfferMirrorRow.venue_offer_id, VenueOfferMirrorRow.present_in_latest_accepted_snapshot,
        VenueOfferMirrorRow.last_accepted_observation_id,
    ).where(*_scoped(VenueOfferMirrorRow, key)))).all()
    if anchor.capture_point:
        live = {row.venue_offer_id for row in mirror if row.present_in_latest_accepted_snapshot}
        stale = {row.venue_offer_id for row in mirror if row.present_in_latest_accepted_snapshot
                 and row.last_accepted_observation_id != anchor.evidence.observation_id}
        violations += _both("legacy_offer_not_live_in_mirror", "live_mirror_offer_not_legacy",
                            ours | foreign, live)
        violations += [_v("live_mirror_not_from_seed", venue_offer_id=v) for v in sorted(stale)]
    else:
        known = {row.venue_offer_id for row in mirror}
        violations += [_v("legacy_offer_without_mirror_row", venue_offer_id=v)
                       for v in sorted((ours | foreign) - known)]
    return ClosureCheck("live_offers", *key, tuple(violations), {
        "mirror_check": "capture_point" if anchor.capture_point else "after_capture_point",
        "legacy_managed": len(ours), "legacy_foreign": len(foreign), "seed_live": len(seed_live),
    })


async def _attempts(session: AsyncSession, key: Key, anchor: _Anchor, seeded: _Seeded) -> ClosureCheck:
    classification = anchor.classification
    reflected = set((classification.get("reflected") or {}).keys())
    settled = set(classification.get("settled") or ())
    legacy = dict((await session.execute(select(
        SubmissionAttemptRow.attempt_id, SubmissionAttemptRow.outcome_kind,
    ).where(*_scoped(SubmissionAttemptRow, key)))).tuples().all())
    violations: list[dict[str, object]] = []
    expected: dict[UUID, tuple[str, str]] = {}
    for attempt_id, kind in legacy.items():
        mapped = LEDGER_OUTCOME_OF_LEGACY.get(str(kind))
        if mapped is None:
            violations.append(_v("legacy_attempt_without_outcome", attempt_id=attempt_id))
            continue
        name = str(attempt_id)
        cls = "reflected" if name in reflected else "settled" if name in settled else "unresolved"
        expected[attempt_id] = (cls, mapped)
    seeded_legacy = {
        attempt_id: (seeded.classes.get(attempt_id, ""), seeded.outcomes.get(attempt_id, ("", None))[0])
        for attempt_id, row in seeded.attempts.items()
        if isinstance(row.seed_provenance, dict) and row.seed_provenance.get("legacy") == "submission_attempt"
    }
    unresolved_legacy = {a for a, (cls, _) in expected.items() if cls == "unresolved"}
    unresolved_seeded = {a for a, (cls, _) in seeded_legacy.items() if cls == "unresolved"}
    violations += _both("legacy_unsettled_attempt_not_seeded_unresolved",
                        "seeded_unresolved_attempt_not_legacy_unsettled",
                        unresolved_legacy, unresolved_seeded)
    violations += _both("legacy_attempt_not_seeded", "seeded_attempt_not_legacy",
                        set(expected), set(seeded_legacy))
    for attempt_id in sorted(set(expected) & set(seeded_legacy), key=str):
        if expected[attempt_id] != seeded_legacy[attempt_id]:
            violations.append(_v("attempt_class_or_kind_differs", attempt_id=attempt_id,
                                 legacy=list(expected[attempt_id]), seeded=list(seeded_legacy[attempt_id])))
    return ClosureCheck("attempts", *key, tuple(violations), {
        "legacy": len(expected), "unresolved": len(unresolved_legacy),
    })


async def _open_uncertainties(session: AsyncSession, key: Key, uncertainties: LedgerUncertainties) -> ClosureCheck:
    legacy = {
        (symbol, attempt_id if kind == "submit_outcome_unknown" else uncertainty_id)
        for uncertainty_id, symbol, kind, attempt_id in await session.execute(select(
            ExecutionUncertaintyRow.uncertainty_id, ExecutionUncertaintyRow.symbol,
            ExecutionUncertaintyRow.kind, ExecutionUncertaintyRow.attempt_id,
        ).where(*_scoped(ExecutionUncertaintyRow, key), ExecutionUncertaintyRow.state == "open"))
    }
    ledger = {(u.symbol, u.subject_id) for u in await uncertainties.open_uncertainties(session, Scope(*key))}
    return ClosureCheck("open_uncertainties", *key, tuple(_both(
        "legacy_open_uncertainty_not_in_ledger", "ledger_open_uncertainty_not_in_legacy", legacy, ledger,
    )), {"legacy": len(legacy), "ledger": len(ledger)})


def _legacy_group(credit_id: str, entry: Mapping[str, Any]) -> CreditGroup:
    basis = LEDGER_ATTRIBUTION_OF_LEGACY.get(str(entry.get("basis")), f"unknown:{entry.get('basis')}")
    period, opening = entry.get("period"), entry.get("opening")
    return CreditGroup(
        str(entry.get("symbol")), _amount(entry.get("amount")) or Decimal(-1),
        None if period is None else int(period), None if opening is None else int(opening),
        basis, frozenset(str(cell) for cell in entry.get("cells") or ()),
    )


async def _credit_groups(session: AsyncSession, key: Key, anchor: _Anchor) -> ClosureCheck:
    violations: list[dict[str, object]] = []
    legacy: dict[tuple[str, str], CreditGroup] = {}
    for credit_id, entry in (anchor.classification.get("credit_cells") or {}).items():
        credit_key = legacy_credit_key(credit_id)
        if credit_key is None:
            violations.append(_v("legacy_credit_id_invalid", credit_id=credit_id))
        else:
            legacy[credit_key] = _legacy_group(credit_id, entry)
    seeded = await basis_groups(session, anchor.evidence.basis_id)
    violations += _both("legacy_group_not_seeded", "seeded_group_not_legacy",
                        {f"{k}:{v}" for k, v in legacy}, {f"{k}:{v}" for k, v in seeded})
    for group_key in sorted(legacy.keys() & seeded.keys()):
        if legacy[group_key] != seeded[group_key]:
            left, right = legacy[group_key], seeded[group_key]
            violations.append(_v("credit_group_differs", credit=f"{group_key[0]}:{group_key[1]}",
                                 legacy=_group_json(left), seeded=_group_json(right)))
    return ClosureCheck("credit_groups", *key, tuple(violations), {"groups": len(legacy)})


def _group_json(group: CreditGroup) -> dict[str, object]:
    return {"symbol": group.symbol, "amount": format(group.amount, "f"), "period_days": group.period_days,
            "mts_opening": group.mts_opening, "attribution_basis": group.attribution_basis,
            "cells": sorted(group.cells)}


async def _symbols(session: AsyncSession, key: Key, anchor: _Anchor) -> ClosureCheck:
    basis_id = anchor.evidence.basis_id
    rows = {row.symbol: row for row in (await session.execute(select(
        AcceptedCapitalBasisSymbolRow.symbol, AcceptedCapitalBasisSymbolRow.available,
        AcceptedCapitalBasisSymbolRow.offered, AcceptedCapitalBasisSymbolRow.credits,
        AcceptedCapitalBasisSymbolRow.unattributed_credits, AcceptedCapitalBasisSymbolRow.foreign_offers,
    ).where(AcceptedCapitalBasisSymbolRow.basis_id == basis_id))).all()}
    cells: dict[str, dict[str, Decimal]] = {}
    for symbol, cell, amount in await session.execute(select(
        AcceptedCapitalBasisCellRow.symbol, AcceptedCapitalBasisCellRow.cell_id,
        AcceptedCapitalBasisCellRow.amount,
    ).where(AcceptedCapitalBasisCellRow.basis_id == basis_id)):
        if amount != _ZERO:
            cells.setdefault(symbol, {})[cell] = amount
    legacy: Mapping[str, Mapping[str, Any]] = anchor.classification.get("symbols") or {}
    violations = _both("legacy_symbol_not_seeded", "seeded_symbol_not_legacy", set(legacy), set(rows))
    for symbol in sorted(set(legacy) & set(rows)):
        values, row = legacy[symbol], rows[symbol]
        pairs = (("available", row.available), ("offered", row.offered), ("credits", row.credits),
                 ("unattributed_credits", row.unattributed_credits), ("foreign", row.foreign_offers))
        for name, seeded_value in pairs:
            if _amount(values.get(name)) != seeded_value:
                violations.append(_v("symbol_value_differs", symbol=symbol, field=name,
                                     legacy=values.get(name), seeded=seeded_value))
        legacy_cells = {str(cell): _amount(amount) for cell, amount in (values.get("cells") or {}).items()
                        if _amount(amount) != _ZERO}
        if legacy_cells != cells.get(symbol, {}):
            violations.append(_v("symbol_cells_differ", symbol=symbol,
                                 legacy={c: _plain(a) for c, a in sorted(legacy_cells.items())},
                                 seeded={c: _plain(a) for c, a in sorted(cells.get(symbol, {}).items())}))
    return ClosureCheck("symbols", *key, tuple(violations), {"symbols": len(legacy)})


async def _seed_provenance(session: AsyncSession, key: Key, anchor: _Anchor, seeded: _Seeded) -> ClosureCheck:
    violations: list[dict[str, object]] = []
    legacy_attempts = {row.attempt_id: row for row in (await session.execute(select(
        SubmissionAttemptRow.attempt_id, SubmissionAttemptRow.execution_decision_id,
    ).where(*_scoped(SubmissionAttemptRow, key)))).all()}
    claims = {(row.cid, row.execution_decision_id): row for row in (await session.execute(select(
        OfferClaimRow.cid, OfferClaimRow.execution_decision_id, OfferClaimRow.venue_offer_id,
    ).where(*_scoped(OfferClaimRow, key)))).all()}
    for attempt_id, row in sorted(seeded.attempts.items(), key=lambda item: str(item[0])):
        provenance = row.seed_provenance
        if not isinstance(provenance, dict):
            violations.append(_v("seeded_attempt_without_provenance", attempt_id=attempt_id))
            continue
        if provenance.get("legacy") == "submission_attempt":
            legacy = legacy_attempts.get(attempt_id)
            if (legacy is None or provenance.get("attempt_id") != str(attempt_id)
                    or legacy.execution_decision_id != row.execution_decision_id):
                violations.append(_v("provenance_attempt_unresolved", attempt_id=attempt_id))
        elif provenance.get("legacy") == "offer_claim":
            claim = claims.get((provenance.get("cid"), provenance.get("execution_decision_id")))
            if (claim is None or claim.execution_decision_id != row.execution_decision_id
                    or claim.venue_offer_id != provenance.get("venue_offer_id")
                    or seeded.offer_owner(attempt_id) != claim.venue_offer_id):
                violations.append(_v("provenance_claim_unresolved", attempt_id=attempt_id))
        else:
            violations.append(_v("provenance_kind_unknown", attempt_id=attempt_id))
    outside = list(await session.scalars(select(SubmissionAttemptJournalRow.attempt_id).where(
        *_scoped(SubmissionAttemptJournalRow, key), SubmissionAttemptJournalRow.seed_provenance.is_not(None),
        SubmissionAttemptJournalRow.basis_id != anchor.evidence.basis_id)))
    violations += [_v("provenance_outside_seed_basis", attempt_id=a) for a in sorted(outside, key=str)]
    quarantines = set(await session.scalars(select(AcceptedCapitalBasisQuarantineRow.quarantine_id).where(
        AcceptedCapitalBasisQuarantineRow.basis_id == anchor.evidence.basis_id)))
    if quarantines:
        known = set(await session.scalars(select(ExecutionUncertaintyRow.uncertainty_id).where(
            *_scoped(ExecutionUncertaintyRow, key),
            ExecutionUncertaintyRow.uncertainty_id.in_(sorted(quarantines)))))
        violations += [_v("seeded_quarantine_unresolved", quarantine_id=q)
                       for q in sorted(quarantines - known, key=str)]
    return ClosureCheck("seed_provenance", *key, tuple(violations), {
        "attempts": len(seeded.attempts), "quarantines": len(quarantines),
    })


async def _fingerprints(
    session: AsyncSession, key: Key, anchor: _Anchor, seeded: _Seeded, managed: LedgerManagedOffers,
) -> ClosureCheck:
    classification = anchor.classification
    # The book as the ledger sees it now: the seed's at the capture point, the runner's after.
    live = set(await session.scalars(select(VenueOfferMirrorRow.venue_offer_id).where(
        *_scoped(VenueOfferMirrorRow, key), VenueOfferMirrorRow.present_in_latest_accepted_snapshot)))
    symbols = set(classification.get("symbols") or {})
    violations: list[dict[str, object]] = []
    classified: list[dict[str, object]] = []
    for symbol in sorted(symbols):
        legacy = await fingerprints_in_use(session, account_id=key[0], environment=key[1], symbol=symbol)
        amounts = await managed.fingerprints_in_use(session, Scope(*key), symbol)
        ledger = frozenset(f for f in (fingerprint_of(a) for a in amounts) if f)
        gone_claims = {
            fingerprint_of(size)
            for size, venue in await session.execute(select(
                OfferClaimRow.size_usdt, OfferClaimRow.venue_offer_id,
            ).where(*_scoped(OfferClaimRow, key), OfferClaimRow.symbol == symbol,
                    OfferClaimRow.state.in_(_OPEN_CLAIMS)))
            if venue is not None and venue not in live
        }
        ack_unreflected = {
            fingerprint_of(row.intended_amount)
            for attempt_id, row in seeded.attempts.items()
            if row.symbol == symbol and seeded.classes.get(attempt_id) == "unresolved"
            and seeded.offer_owner(attempt_id) is not None
        }
        for fingerprint in sorted(legacy - ledger):
            if fingerprint in gone_claims:
                classified.append({"symbol": symbol, "fingerprint": fingerprint,
                                   "side": "legacy", "class": "legacy_claim_offer_gone"})
            else:
                violations.append(_v("fingerprint_legacy_only", symbol=symbol, fingerprint=fingerprint))
        for fingerprint in sorted(ledger - legacy):
            if fingerprint in ack_unreflected:
                classified.append({"symbol": symbol, "fingerprint": fingerprint,
                                   "side": "ledger", "class": "ack_unreflected"})
            else:
                violations.append(_v("fingerprint_ledger_only", symbol=symbol, fingerprint=fingerprint))
    return ClosureCheck("fingerprints", *key, tuple(violations), {"classified": classified})


async def _trading_state(session: AsyncSession, key: Key, anchor: _Anchor) -> ClosureCheck:
    latest = await session.scalar(select(func.max(TradingStateRow.id)).where(*_scoped(TradingStateRow, key)))
    expected = anchor.evidence.trading_state_max_id
    violations = () if latest == expected else (_v("trading_state_moved", found=latest, expected=expected),)
    return ClosureCheck("trading_state", *key, violations, {"latest_id": latest})


async def _requests(session: AsyncSession, key: Key, anchor: _Anchor) -> ClosureCheck:
    evidence = anchor.evidence
    rows = {row.request_id: row for row in (await session.execute(select(
        UncertaintyResolutionRequestRow.request_id, UncertaintyResolutionRequestRow.state,
        UncertaintyResolutionRequestRow.outcome_reason,
    ).where(*_scoped(UncertaintyResolutionRequestRow, key)))).all()}
    violations = [_v("uncertainty_request_pending", request_id=r) for r, row in sorted(rows.items(), key=lambda i: str(i[0]))
                  if row.state == "requested"]
    for request_id in sorted(evidence.failed_uncertainty_requests, key=str):
        row = rows.get(request_id)
        if row is None or (row.state, row.outcome_reason) != ("failed", SUPERSEDED_REASON):
            violations.append(_v("failed_request_not_terminal", request_id=request_id,
                                 state=None if row is None else row.state))
    superseded = {r for r, row in rows.items() if row.outcome_reason == SUPERSEDED_REASON}
    violations += [_v("superseded_request_not_in_evidence", request_id=r)
                   for r in sorted(superseded - evidence.failed_uncertainty_requests, key=str)]
    counts: dict[str, int] = {}
    for name, table in (("capital_policy_requests", CapitalPolicyRequestRow),
                        ("trading_control_requests", TradingControlRequestRow)):
        pending = set(await session.scalars(select(table.request_id).where(
            *_scoped(table, key), table.state == "requested")))
        counts[name] = len(pending)
        violations += _both("carried_request_not_pending", "pending_request_not_carried",
                            set(evidence.carried_requests[name]), pending, table=name)
    return ClosureCheck("requests", *key, tuple(violations), {
        "failed": len(evidence.failed_uncertainty_requests), "pending": counts,
    })


async def _projection_symbols(session: AsyncSession, key: Key, cells: set[str]) -> ClosureCheck:
    found: set[tuple[str, str]] = set()
    for table, row_type in (("venue_offer_state", VenueOfferStateRow), ("venue_credit_state", VenueCreditStateRow)):
        found.update((table, symbol) for symbol in await session.scalars(select(row_type.symbol).where(
            *_scoped(row_type, key), row_type.is_terminal.is_(False)).distinct()))
    violations = [_v("live_projection_symbol_without_cell", table=table, symbol=symbol)
                  for table, symbol in sorted(found) if symbol not in cells]
    return ClosureCheck("projection_symbols", *key, tuple(violations), {"live_symbols": len(found)})


async def verify_closure(
    session: AsyncSession, *, scopes: Sequence[CapitalScope], seeds: Mapping[Key, SeedEvidence],
    managed_offers: LedgerManagedOffers, uncertainties: LedgerUncertainties,
) -> tuple[ClosureCheck, ...]:
    """Every check for every listed (account, environment), in the caller's snapshot."""
    listed: dict[Key, set[str]] = {}
    for scope in scopes:
        listed.setdefault((scope.account_id, scope.environment), set()).add(scope.symbol)
    results: list[ClosureCheck] = []
    for key in sorted(seeds.keys() - listed.keys(), key=str):
        results.append(ClosureCheck("seed_anchor", *key, (_v("seed_evidence_unlisted"),)))
    for key in sorted(listed, key=str):
        anchor, problems = await _anchor(session, key, seeds.get(key))
        results.append(ClosureCheck("seed_anchor", *key, tuple(problems)))
        if anchor is None:
            results.extend(ClosureCheck(name, *key, (_v("seed_anchor_unavailable"),))
                           for name in CHECKS[1:])
            continue
        seeded = await _seeded(session, anchor.evidence.basis_id)
        results.append(await _live_offers(session, key, anchor, seeded))
        results.append(await _attempts(session, key, anchor, seeded))
        results.append(await _open_uncertainties(session, key, uncertainties))
        results.append(await _credit_groups(session, key, anchor))
        results.append(await _symbols(session, key, anchor))
        results.append(await _seed_provenance(session, key, anchor, seeded))
        results.append(await _fingerprints(session, key, anchor, seeded, managed_offers))
        results.append(await _trading_state(session, key, anchor))
        results.append(await _requests(session, key, anchor))
        results.append(await _projection_symbols(session, key, listed[key]))
    return tuple(results)


def summarize_closure(scopes: Sequence[CapitalScope], checks: Sequence[ClosureCheck]) -> dict[str, Any]:
    keys = {(scope.account_id, scope.environment) for scope in scopes}
    ran = {(c.account_id, c.environment, c.check) for c in checks}
    complete = bool(keys) and all((k[0], k[1], name) in ran for k in keys for name in CHECKS)
    violations = sum(len(c.violations) for c in checks)
    return {"checks": len(checks), "violations": violations, "coverage_complete": complete,
            "passed": complete and violations == 0}


def closure_json(check: ClosureCheck) -> dict[str, Any]:
    return {"kind": "closure", "check": check.check,
            "scope": {"account_id": str(check.account_id), "environment": check.environment},
            "status": check.status, "violations": list(check.violations),
            "evidence": json.loads(json.dumps(check.evidence, default=str))}


__all__ = [
    "CARRIED_TABLES",
    "CHECKS",
    "LEDGER_ATTRIBUTION_OF_LEGACY",
    "LEDGER_OUTCOME_OF_LEGACY",
    "SUPERSEDED_REASON",
    "ClosureCheck",
    "EvidenceRejectedError",
    "SeedEvidence",
    "closure_json",
    "parse_seed_evidence",
    "summarize_closure",
    "verify_closure",
]
