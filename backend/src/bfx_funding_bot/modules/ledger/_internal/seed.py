"""The one-time legacy closure seed: plan the rows from a closure, then write them (owner only).

``plan_seed`` is pure: it validates the closure against the invariants the ledger relies on
and returns every row the seed writes, keyed by table, with exactly the canonical columns of
``ledger.table_digest`` (no generated column), so the same rows are both the expected digest
and the insert. Identifiers the closure does not carry (query, observation, basis, detail
rows) are uuid5 of the scope and the legacy snapshot, so a seed of the same closure, on the
isolated DR copy or on the database itself, writes the same bytes.

``write_plan`` inserts the rows in an order the triggers and foreign keys allow:

1. clock at revision 0, then the query (revision 1, start revision 0): the accept fence of
   the observation insert requires ``accept_revision = start_revision = clock`` and the query
   to be the scope's latest;
2. the ``legacy_seed`` observation (accepted) and its active offer and credit rows;
3. the seed basis and its symbol, cell, credit and credit-cell rows;
4. the attempts (they name the basis), their transport outcomes, the basis attempt rows;
5. the quarantine openings, each at the next clock revision like ``open_quarantine``, their
   members (citing the seed observation, which the schema allows) and the basis quarantine
   rows; the clock ends at the last opening's revision;
6. the offer and credit mirrors (live, last accepted observation = the seed);
7. pending ``uncertainty_resolution_requests`` of the scope fail
   ``superseded_by_authority_switch`` (ruling 2026-10-02). The other request tables carry.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import column, exists, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import BigInteger, Text, Uuid

from bfx_funding_bot.modules.ledger import (
    ATTRIBUTION_BASES,
    SEED_CLASSIFICATIONS,
    JsonObject,
    Scope,
    SeedClosure,
    SeedRefused,
)
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisCellRow,
    AcceptedCapitalBasisCreditCellRow,
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisQuarantineRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    CapitalCommandClockRow,
    ExecutionResolutionJournalRow,
    LedgerObservationCreditRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    QuarantineMemberRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
    VenueCreditMirrorRow,
    VenueOfferMirrorRow,
)

SEED_SCHEMA_VERSION = 1
SUPERSEDED_REASON = "superseded_by_authority_switch"
# Fixed: every seed id is uuid5(namespace, scope | legacy snapshot | kind | key).
SEED_NAMESPACE = UUID("6f1d3f0e-0c4b-5e5a-9d2b-1c7a5e0b4d21")
ZERO = Decimal(0)

type Row = dict[str, object]

# The request outbox is execution's table; ledger names only what the failure writes.
_REQUESTS = table(
    "uncertainty_resolution_requests",
    column("request_id", Uuid),
    column("exchange_account_id", Uuid),
    column("deployment_environment", Text),
    column("state", Text),
    column("processed_at_ms", BigInteger),
    column("outcome_reason", Text),
)

# Scoped parents: no child row can exist in a scope without one of these (foreign keys).
_SCOPE_TABLES = (
    CapitalCommandClockRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    VenueOfferMirrorRow,
    VenueCreditMirrorRow,
    SubmissionAttemptJournalRow,
    QuarantineOpeningRow,
    ExecutionResolutionJournalRow,
    AcceptedCapitalBasisRow,
)

# Insert order (see the module docstring); the clock is inserted at 0 and set last.
_INSERT_ORDER = (
    "ledger_observation_query",
    "ledger_observation",
    "ledger_observation_offer",
    "ledger_observation_credit",
    "accepted_capital_basis",
    "accepted_capital_basis_symbol",
    "accepted_capital_basis_cell",
    "accepted_capital_basis_credit",
    "accepted_capital_basis_credit_cell",
    "submission_attempt_journal",
    "transport_outcome_journal",
    "accepted_capital_basis_attempt",
    "quarantine_opening",
    "quarantine_member",
    "accepted_capital_basis_quarantine",
    "venue_offer_mirror",
    "venue_credit_mirror",
)
_TABLES: dict[str, Any] = {
    row.__tablename__: row.__table__
    for row in (
        CapitalCommandClockRow,
        LedgerObservationQueryRow,
        LedgerObservationRow,
        LedgerObservationOfferRow,
        LedgerObservationCreditRow,
        AcceptedCapitalBasisRow,
        AcceptedCapitalBasisSymbolRow,
        AcceptedCapitalBasisCellRow,
        AcceptedCapitalBasisCreditRow,
        AcceptedCapitalBasisCreditCellRow,
        SubmissionAttemptJournalRow,
        TransportOutcomeJournalRow,
        AcceptedCapitalBasisAttemptRow,
        QuarantineOpeningRow,
        QuarantineMemberRow,
        AcceptedCapitalBasisQuarantineRow,
        VenueOfferMirrorRow,
        VenueCreditMirrorRow,
    )
}


@dataclass(frozen=True, slots=True)
class SeedPlan:
    """Every row of the seed (canonical columns, final values) and the requests it fails."""

    scope: Scope
    query_id: UUID
    observation_id: UUID
    basis_id: UUID
    rows: Mapping[str, tuple[Row, ...]]
    pending_uncertainty_requests: tuple[UUID, ...]

    def table_rows(self, name: str) -> tuple[Row, ...]:
        return self.rows.get(name, ())


def _text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _amount(payload: Mapping[str, object]) -> Decimal | None:
    try:
        value = Decimal(str(payload["amount"]))
    except (KeyError, InvalidOperation):
        return None
    return value if value.is_finite() and value >= 0 else None


def _json(value: object) -> JsonObject:
    """A JSON object exactly as it will be stored (refuses what canonical JSON refuses)."""
    if not isinstance(value, dict):
        raise SeedRefused("closure_json_invalid")
    try:
        decoded: JsonObject = json.loads(canonical_payload(value))
    except (TypeError, ValueError):
        raise SeedRefused("closure_json_invalid") from None
    return decoded


def _seed_id(closure: SeedClosure, kind: str, key: str) -> UUID:
    scope = closure.scope
    return uuid5(
        SEED_NAMESPACE,
        f"{scope.exchange_account_id}|{scope.deployment_environment}|"
        f"{closure.watermarks.snapshot_event_seq}|{kind}|{key}",
    )


def _unique[T](items: Iterable[T], key: Any, reason: str) -> dict[Any, T]:
    found: dict[Any, T] = {}
    for item in items:
        identity = key(item)
        if identity in found:
            raise SeedRefused(reason, str(identity))
        found[identity] = item
    return found


def _validate(closure: SeedClosure) -> None:
    """The invariants the first real basis relies on; any break is a refusal, never a patch."""
    offers = _unique(closure.offers, lambda o: o.venue_offer_id, "duplicate_offer")
    credits = _unique(
        closure.credits, lambda c: (c.source_kind, c.venue_credit_id), "duplicate_credit"
    )
    symbols = _unique(closure.symbols, lambda s: s.symbol, "duplicate_symbol")
    groups = _unique(
        closure.credit_groups, lambda g: (g.source_kind, g.venue_credit_id), "duplicate_group"
    )
    attempts = _unique(closure.attempts, lambda a: a.attempt_id, "duplicate_attempt")
    _unique(closure.attempts, lambda a: a.attempt_seq, "duplicate_attempt_seq")
    _unique(closure.attempts, lambda a: a.execution_decision_id, "duplicate_attempt_decision")
    _unique(closure.quarantines, lambda q: q.quarantine_id, "duplicate_quarantine")
    if closure.confirmation_finished_at_ms < closure.query_finished_at_ms or not (
        0 <= closure.query_started_at_ms <= closure.query_finished_at_ms
    ):
        raise SeedRefused("closure_times_invalid")
    for offer in offers.values():
        if offer.symbol not in symbols:
            raise SeedRefused("symbol_missing", offer.symbol)
        if not (ZERO < offer.amount_remaining <= offer.amount_original):
            raise SeedRefused("offer_amount_invalid", offer.venue_offer_id)
    for credit in credits.values():
        if credit.symbol not in symbols:
            raise SeedRefused("symbol_missing", credit.symbol)
        if credit.amount <= ZERO or credit.period_days <= 0 or credit.mts_opening < 0:
            raise SeedRefused("credit_invalid", credit.venue_credit_id)
    # Credit groups <-> live credits, both directions, same key and amount.
    if set(groups) != set(credits):
        raise SeedRefused("credit_group_mismatch")
    for key, group in groups.items():
        credit = credits[key]
        if (group.symbol, group.amount, group.period_days, group.mts_opening) != (
            credit.symbol, credit.amount, credit.period_days, credit.mts_opening
        ):
            raise SeedRefused("credit_group_mismatch", group.venue_credit_id)
        if group.attribution_basis not in ATTRIBUTION_BASES:
            raise SeedRefused("attribution_basis_unknown", group.venue_credit_id)
        # A trade or carry may name no cell of ours (trades naming foreign offers only);
        # unattributed never names one.
        if group.attribution_basis == "unattributed" and group.cells:
            raise SeedRefused("attribution_cells_mismatch", group.venue_credit_id)
    # Attempts: outcome shape, classification, payload, live provenance.
    owners: dict[str, list[UUID]] = defaultdict(list)
    for attempt in attempts.values():
        outcome = attempt.outcome
        if attempt.classification not in SEED_CLASSIFICATIONS:
            raise SeedRefused("attempt_classification_unknown", str(attempt.attempt_id))
        if outcome.kind not in ("ack", "rejected", "not_sent", "unknown") or (
            (outcome.kind == "ack") != (outcome.venue_offer_id is not None)
        ):
            raise SeedRefused("attempt_outcome_invalid", str(attempt.attempt_id))
        if attempt.attempt_seq < 0 or attempt.started_at_ms < 0 or outcome.completed_at_ms < 0:
            raise SeedRefused("attempt_order_invalid", str(attempt.attempt_id))
        if _amount(attempt.normalized_payload) is None:
            raise SeedRefused("attempt_amount_invalid", str(attempt.attempt_id))
        if not attempt.provenance:
            raise SeedRefused("attempt_provenance_missing", str(attempt.attempt_id))
        if outcome.venue_offer_id is not None:
            owners[outcome.venue_offer_id].append(attempt.attempt_id)
    # Totals: the symbol rows must be what the offers, credits and provenance add up to.
    offered: dict[str, Decimal] = defaultdict(lambda: ZERO)
    foreign: dict[str, Decimal] = defaultdict(lambda: ZERO)
    cells: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for offer in offers.values():
        named = owners.get(offer.venue_offer_id, [])
        if len(named) > 1:
            raise SeedRefused("offer_provenance_conflict", offer.venue_offer_id)
        if not named:
            foreign[offer.symbol] += offer.amount_remaining
            continue
        owner = attempts[named[0]]
        if owner.symbol != offer.symbol or owner.classification != "reflected":
            raise SeedRefused("offer_provenance_conflict", offer.venue_offer_id)
        if _amount(owner.normalized_payload) != offer.amount_original:
            raise SeedRefused("offer_amount_conflict", offer.venue_offer_id)
        offered[offer.symbol] += offer.amount_remaining
        cells[(offer.symbol, owner.cell_id)] += offer.amount_remaining
    lent: dict[str, Decimal] = defaultdict(lambda: ZERO)
    unattributed: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for group in groups.values():
        lent[group.symbol] += group.amount
        if not group.cells:
            unattributed[group.symbol] += group.amount
        for cell in group.cells:
            cells[(group.symbol, cell)] += group.amount
    for name, row in symbols.items():
        if (row.offered, row.foreign_offers, row.credits, row.unattributed_credits) != (
            offered[name], foreign[name], lent[name], unattributed[name]
        ):
            raise SeedRefused("classification_inconsistent", name)
        expected = {cell: amount for (s, cell), amount in cells.items() if s == name}
        if {cell: amount for cell, amount in row.cells.items() if amount != ZERO} != {
            cell: amount for cell, amount in expected.items() if amount != ZERO
        }:
            raise SeedRefused("classification_cells_inconsistent", name)
        if min(row.available, row.offered, row.credits, row.foreign_offers) < ZERO:
            raise SeedRefused("classification_inconsistent", name)
    for quarantine in closure.quarantines:
        if quarantine.intended_amount < ZERO or quarantine.opened_at_ms < 0:
            raise SeedRefused("quarantine_invalid", str(quarantine.quarantine_id))
        for member in quarantine.members:
            if member.source_kind == "offer":
                seen = offers.get(member.venue_object_id)
                symbol = seen.symbol if seen is not None else None
            else:
                live = credits.get((member.source_kind, member.venue_object_id))
                symbol = live.symbol if live is not None else None
            if symbol != quarantine.symbol:
                raise SeedRefused("quarantine_member_unobserved", member.venue_object_id)


def _observation_digest(closure: SeedClosure) -> str:
    payload: JsonObject = {
        "domain": "bfx-ledger-observation-legacy-seed",
        "version": SEED_SCHEMA_VERSION,
        "legacy_snapshot_event_seq": closure.watermarks.snapshot_event_seq,
        "legacy_snapshot_query_id": str(closure.watermarks.snapshot_query_id),
        "symbols": [[s.symbol, _text(s.available)] for s in sorted(closure.symbols, key=lambda s: s.symbol)],
        "offers": [
            [o.venue_offer_id, o.symbol, _text(o.amount_original), _text(o.amount_remaining)]
            for o in sorted(closure.offers, key=lambda o: o.venue_offer_id)
        ],
        "credits": [
            [c.source_kind, c.venue_credit_id, c.symbol, _text(c.amount), c.period_days, c.mts_opening]
            for c in sorted(closure.credits, key=lambda c: (c.source_kind, c.venue_credit_id))
        ],
    }
    return sha256(canonical_payload(payload)).hexdigest()


def plan_seed(closure: SeedClosure) -> SeedPlan:
    """Validate ``closure`` and return every row the seed writes (pure, deterministic)."""
    _validate(closure)
    scope = closure.scope
    account, environment = scope.exchange_account_id, scope.deployment_environment
    query_id = _seed_id(closure, "query", "")
    observation_id = _seed_id(closure, "observation", "")
    basis_id = _seed_id(closure, "basis", "")
    marks = closure.watermarks
    raw: JsonObject = {"origin": "legacy_seed", "legacy_snapshot_event_seq": marks.snapshot_event_seq}
    digest = _observation_digest(closure)
    rows: dict[str, list[Row]] = defaultdict(list)

    rows["ledger_observation_query"].append({
        "query_id": query_id, "exchange_account_id": account,
        "deployment_environment": environment, "query_revision": 1,
        "started_at_ms": closure.query_started_at_ms, "start_revision": 0,
    })
    rows["ledger_observation"].append({
        "id": observation_id, "query_id": query_id, "exchange_account_id": account,
        "deployment_environment": environment, "schema_version": 1,
        "query_finished_at_ms": closure.query_finished_at_ms,
        "confirmation_finished_at_ms": closure.confirmation_finished_at_ms,
        "accept_revision": 0, "origin": "legacy_seed",
        # The legacy snapshot covers live offers, credits and loans, and nothing else.
        "wallets_complete": False, "offers_complete": True, "credits_complete": True,
        "loans_complete": True, "offer_history_complete": False,
        "credit_history_complete": False, "trades_complete": False,
        "trades_requested_start_ms": None, "trades_requested_end_ms": None,
        "offer_history_pages": None, "credit_history_pages": None,
        "history_requested_start_ms": None, "history_requested_end_ms": None,
        "history_oldest_mts_created": None, "history_newest_mts_created": None,
        "first_digest": digest, "confirmation_digest": digest, "accepted": True,
        "evidence": _json({
            "legacy_seed": {
                "final_event_seq": marks.final_event_seq,
                "snapshot_event_seq": marks.snapshot_event_seq,
                "snapshot_query_id": str(marks.snapshot_query_id),
                "snapshot_command_fence": marks.snapshot_command_fence,
                "trading_state_max_id": marks.trading_state_max_id,
                **closure.evidence,
            },
        }),
    })
    for offer in sorted(closure.offers, key=lambda o: o.venue_offer_id):
        detail = {
            "venue_offer_id": offer.venue_offer_id, "symbol": offer.symbol,
            "amount_original": offer.amount_original, "amount_remaining": offer.amount_remaining,
            "rate": offer.rate, "rate_observed": offer.rate is not None,
            "period_days": offer.period_days, "offer_type": offer.offer_type,
            "flags": None if offer.flags is None else _json(offer.flags), "status": offer.status,
            "mts_created": offer.mts_created, "mts_updated": offer.mts_updated,
        }
        rows["ledger_observation_offer"].append({
            "id": _seed_id(closure, "offer", offer.venue_offer_id),
            "observation_id": observation_id, **detail, "raw": raw,
        })
        rows["venue_offer_mirror"].append({
            "exchange_account_id": account, "deployment_environment": environment, **detail,
            "last_accepted_observation_id": observation_id,
            "present_in_latest_accepted_snapshot": True,
            "terminal_evidence_id": None, "terminal_kind": None,
        })
    for credit in sorted(closure.credits, key=lambda c: (c.source_kind, c.venue_credit_id)):
        detail = {
            "venue_credit_id": credit.venue_credit_id, "source_kind": credit.source_kind,
            "symbol": credit.symbol, "amount": credit.amount, "rate": credit.rate,
            "period_days": credit.period_days, "status": credit.status,
            "flags": None if credit.flags is None else _json(credit.flags),
            "mts_created": credit.mts_created, "mts_updated": credit.mts_updated,
            "mts_opening": credit.mts_opening,
        }
        rows["ledger_observation_credit"].append({
            "id": _seed_id(closure, "credit", f"{credit.source_kind}|{credit.venue_credit_id}"),
            "observation_id": observation_id, **detail, "raw": raw,
        })
        rows["venue_credit_mirror"].append({
            "exchange_account_id": account, "deployment_environment": environment, **detail,
            "last_accepted_observation_id": observation_id,
            "present_in_latest_accepted_snapshot": True,
            "terminal_evidence_id": None, "terminal_kind": None,
        })

    high_water = max((a.attempt_seq for a in closure.attempts), default=0)
    attempts = sorted(closure.attempts, key=lambda a: a.attempt_seq)
    quarantines = sorted(closure.quarantines, key=lambda q: (q.opened_at_ms, str(q.quarantine_id)))
    symbols = sorted(closure.symbols, key=lambda s: s.symbol)
    groups = sorted(closure.credit_groups, key=lambda g: (g.source_kind, g.venue_credit_id))
    cells = sorted(
        (s.symbol, cell, amount) for s in symbols for cell, amount in s.cells.items()
    )
    basis_payload: JsonObject = {
        "domain": "bfx-ledger-capital-basis",
        "version": 1,
        "origin": "legacy_seed",
        "observation_id": str(observation_id),
        "accept_revision": 0,
        "attempt_seq_high_water": high_water,
        "scope_block": None,
        "symbols": [
            [s.symbol, _text(s.available), _text(s.offered), _text(s.credits),
             _text(s.unattributed_credits), _text(s.foreign_offers), None, "baseline", "0", "0", 0]
            for s in symbols
        ],
        "cells": [[s, cell, _text(amount)] for s, cell, amount in cells],
        "credits": [
            [g.source_kind, g.venue_credit_id, g.symbol, _text(g.amount), g.period_days,
             g.mts_opening, g.attribution_basis, sorted(g.cells)]
            for g in groups
        ],
        "attempts": [
            [str(a.attempt_id), a.symbol, a.classification]
            for a in sorted(attempts, key=lambda a: str(a.attempt_id))
        ],
        "quarantines": [str(q.quarantine_id) for q in quarantines],
    }
    rows["accepted_capital_basis"].append({
        "id": basis_id, "exchange_account_id": account, "deployment_environment": environment,
        "observation_id": observation_id, "accepted": True, "accept_revision": 0,
        "attempt_seq_high_water": high_water, "scope_block": None, "schema_version": 1,
        "digest": sha256(canonical_payload(basis_payload)).hexdigest(),
        "accepted_at_ms": closure.confirmation_finished_at_ms,
    })
    for s in symbols:
        rows["accepted_capital_basis_symbol"].append({
            "basis_id": basis_id, "symbol": s.symbol, "available": s.available,
            "offered": s.offered, "credits": s.credits,
            "unattributed_credits": s.unattributed_credits, "foreign_offers": s.foreign_offers,
            "block": None, "conservation": "baseline", "lent_unexplained": ZERO,
            "foreign_executed": ZERO, "fill_conflicts": 0,
        })
    for name, cell, amount in cells:
        rows["accepted_capital_basis_cell"].append(
            {"basis_id": basis_id, "symbol": name, "cell_id": cell, "amount": amount}
        )
    for g in groups:
        rows["accepted_capital_basis_credit"].append({
            "basis_id": basis_id, "source_kind": g.source_kind,
            "venue_credit_id": g.venue_credit_id, "symbol": g.symbol, "amount": g.amount,
            "period_days": g.period_days, "mts_opening": g.mts_opening,
            "attribution_basis": g.attribution_basis,
        })
        for cell in sorted(g.cells):
            rows["accepted_capital_basis_credit_cell"].append({
                "basis_id": basis_id, "source_kind": g.source_kind,
                "venue_credit_id": g.venue_credit_id, "cell_id": cell,
            })
    for a in attempts:
        payload = _json(a.normalized_payload)
        rows["submission_attempt_journal"].append({
            "attempt_id": a.attempt_id, "execution_decision_id": a.execution_decision_id,
            "exchange_account_id": account, "deployment_environment": environment,
            "symbol": a.symbol, "cell_id": a.cell_id, "attempt_seq": a.attempt_seq,
            "normalized_payload": payload,
            "payload_sha256": sha256(canonical_payload(payload)).hexdigest(),
            "basis_id": basis_id, "policy_revision_id": None,
            "authorization_evidence": _json({"legacy_seed": True, "basis_id": str(basis_id)}),
            "seed_provenance": _json(a.provenance), "started_at_ms": a.started_at_ms,
        })
        rows["transport_outcome_journal"].append({
            "attempt_id": a.attempt_id, "kind": a.outcome.kind,
            "venue_offer_id": a.outcome.venue_offer_id, "reason": a.outcome.reason,
            "completed_at_ms": a.outcome.completed_at_ms, "evidence": _json(a.outcome.evidence),
        })
        rows["accepted_capital_basis_attempt"].append({
            "basis_id": basis_id, "attempt_id": a.attempt_id, "symbol": a.symbol,
            "classification": a.classification,
        })
    for revision, q in enumerate(quarantines, start=1):
        rows["quarantine_opening"].append({
            "quarantine_id": q.quarantine_id, "exchange_account_id": account,
            "deployment_environment": environment, "symbol": q.symbol,
            "intended_amount": q.intended_amount, "opened_at_ms": q.opened_at_ms,
            "opened_revision": revision, "evidence": _json(q.evidence),
            "legacy_reconcile_event_seq": None, "source_attempt_id": None,
        })
        for m in sorted(q.members, key=lambda m: (m.source_kind, m.venue_object_id)):
            rows["quarantine_member"].append({
                "quarantine_id": q.quarantine_id, "source_kind": m.source_kind,
                "venue_object_id": m.venue_object_id, "observation_id": observation_id,
                "amount_at_join": m.amount_at_join,
            })
        rows["accepted_capital_basis_quarantine"].append(
            {"basis_id": basis_id, "quarantine_id": q.quarantine_id}
        )
    rows["capital_command_clock"].append({
        "exchange_account_id": account, "deployment_environment": environment,
        "revision": len(quarantines),
    })
    return SeedPlan(
        scope, query_id, observation_id, basis_id,
        {name: tuple(values) for name, values in rows.items()},
        tuple(sorted(set(closure.pending_uncertainty_requests), key=str)),
    )


async def ledger_rows_in_scope(session: AsyncSession, scope: Scope) -> tuple[str, ...]:
    """The scoped ledger tables that already hold a row of ``scope`` (children need one)."""
    found: list[str] = []
    for row in _SCOPE_TABLES:
        present = await session.scalar(
            select(
                exists().where(
                    row.exchange_account_id == scope.exchange_account_id,
                    row.deployment_environment == scope.deployment_environment,
                )
            )
        )
        if present:
            found.append(row.__tablename__)
    return tuple(found)


async def write_plan(session: AsyncSession, plan: SeedPlan, *, now_ms: int) -> int:
    """Insert ``plan`` in trigger/FK order; returns the number of requests failed."""
    scope = plan.scope
    occupied = await ledger_rows_in_scope(session, scope)
    if occupied:
        raise SeedRefused("ledger_not_empty", *occupied)
    (clock,) = plan.table_rows("capital_command_clock")
    await session.execute(_TABLES["capital_command_clock"].insert(), [{**clock, "revision": 0}])
    for name in _INSERT_ORDER:
        values = plan.table_rows(name)
        if values:
            await session.execute(_TABLES[name].insert(), [dict(v) for v in values])
    await session.execute(
        update(CapitalCommandClockRow)
        .where(
            CapitalCommandClockRow.exchange_account_id == scope.exchange_account_id,
            CapitalCommandClockRow.deployment_environment == scope.deployment_environment,
        )
        .values(revision=clock["revision"])
    )
    failed = 0
    if plan.pending_uncertainty_requests:
        result = await session.execute(
            update(_REQUESTS)
            .where(
                _REQUESTS.c.request_id.in_(plan.pending_uncertainty_requests),
                _REQUESTS.c.exchange_account_id == scope.exchange_account_id,
                _REQUESTS.c.deployment_environment == scope.deployment_environment,
                _REQUESTS.c.state == "requested",
            )
            .values(state="failed", processed_at_ms=now_ms, outcome_reason=SUPERSEDED_REASON)
        )
        failed = int(result.rowcount or 0)  # type: ignore[attr-defined]
        if failed != len(plan.pending_uncertainty_requests):
            raise SeedRefused("pending_requests_changed")
    left = await session.scalar(
        select(exists().where(
            _REQUESTS.c.exchange_account_id == scope.exchange_account_id,
            _REQUESTS.c.deployment_environment == scope.deployment_environment,
            _REQUESTS.c.state == "requested",
        ))
    )
    if left:
        raise SeedRefused("pending_requests_changed")
    return failed


__all__ = [
    "SEED_NAMESPACE",
    "SUPERSEDED_REASON",
    "SeedPlan",
    "ledger_rows_in_scope",
    "plan_seed",
    "write_plan",
]
