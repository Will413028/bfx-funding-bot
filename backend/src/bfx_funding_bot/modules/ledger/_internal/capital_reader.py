"""Dormant capital read: the latest query's accepted basis, its tail, and policy.

Everything is read in the caller's one REPEATABLE READ READ ONLY transaction, so
the basis, the tail and the clock come from one snapshot. Every statement is
bounded by a key or by the basis:

1. latest query of the scope (``ix_ledger_observation_query_scope_revision``,
   LIMIT 1) -> its observation (unique ``query_id``) -> its basis (unique
   ``observation_id``). The basis is never chosen by time: a newer query
   without an accepted basis makes the read ``snapshot_query_pending`` (or
   ``snapshot_unavailable`` when the scope has no basis at all, LIMIT 1).
2. the clock row (PK); the basis's ``accept_revision`` must not exceed it.
3. the read symbol's basis row and cells (PK prefix ``basis_id, symbol``).
4. the basis's attempt and quarantine rows (PK prefix ``basis_id``).
5. quarantines opened after the basis: ``opened_revision > accept_revision``
   (``ix_quarantine_opening_scope_revision``).
6. tail attempts ``attempt_seq > attempt_seq_high_water``
   (``uq_submission_attempt_scope_seq``, at most ``MAX_TAIL_ATTEMPTS`` + 1 rows;
   more fails closed), plus outcomes (PK) and resolutions (unique partial
   index) of the tail and of the basis's unresolved attempts.
7. the read symbol's policy head and revision (PKs), parsed with its digest.

Policy is read here, per symbol, never at acceptance. There is no venue
timestamp in this read: freshness compares the caller's local ``now_ms`` with
the query's local start, so ``VENUE_CLOCK_TOLERANCE_MS`` does not apply (it is
applied where the basis compares venue stamps).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import CapitalReadRefused, LedgerCapitalRead
from bfx_funding_bot.modules.ledger._internal.attempts import (
    MAX_TAIL_ATTEMPTS,
    attempt_evidence,
    open_unknowns,
    tail_attempts,
)
from bfx_funding_bot.modules.ledger._internal.attempts import fresh_all as _all
from bfx_funding_bot.modules.ledger._internal.attempts import payload_amount as _amount
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisCellRow,
    AcceptedCapitalBasisQuarantineRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    CapitalCommandClockRow,
    CapitalPolicyHeadRow,
    CapitalPolicyRevisionRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    QuarantineOpeningRow,
)
from bfx_funding_bot.modules.trading import (
    AcceptedCapitalBasis,
    AppliedPolicy,
    AttemptFact,
    AttemptOutcome,
    Blocked,
    CapitalReadContext,
    CapitalScope,
    PolicyRejectedError,
    SymbolCapital,
    UncertaintyFact,
    check_pointer,
    derive_capital,
    parse_policy,
)

_NO_QUERY = UUID(int=0)
_OUTCOMES: dict[str, AttemptOutcome] = {
    "ack": "acknowledged",
    "rejected": "rejected",
    "not_sent": "not_sent",
    "unknown": "unknown",
}
_RESOLUTIONS: dict[str, Literal["acknowledged", "not_sent"]] = {
    "bound_to_venue": "acknowledged",
    "not_accepted": "not_sent",
}


async def _require_snapshot_transaction(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text(
                "SELECT current_setting('transaction_isolation'), "
                "current_setting('transaction_read_only')"
            )
        )
    ).one()
    if (row[0], row[1]) != ("repeatable read", "on"):
        raise CapitalReadRefused(
            f"capital read needs REPEATABLE READ READ ONLY, got {row[0]} read_only={row[1]}"
        )


async def _one[T](session: AsyncSession, statement: Select[tuple[T]]) -> T | None:
    rows = await _all(session, statement.limit(1))
    return rows[0] if rows else None


def _blocked(value: Any) -> Blocked | None:
    """A stored ``{"reasons": [...]}`` block; the first sorted reason leads."""
    if value is None:
        return None
    reasons = value.get("reasons") if isinstance(value, dict) else None
    if (
        not isinstance(reasons, list)
        or not reasons
        or not all(
            isinstance(item, dict) and isinstance(item.get("reason"), str) for item in reasons
        )
    ):
        return Blocked("snapshot_evidence_conflict", (("block", "malformed"),))
    evidence = tuple((str(k), str(v)) for item in reasons for k, v in sorted(item.items()))
    return Blocked(reasons[0]["reason"], evidence)


async def _policy(session: AsyncSession, scope: CapitalScope) -> AppliedPolicy | Blocked:
    """The symbol's head -> its revision, proven and parsed (legacy read_policy_row order)."""
    head = (
        await session.execute(
            select(CapitalPolicyHeadRow.revision_id, CapitalPolicyHeadRow.revision).where(
                CapitalPolicyHeadRow.exchange_account_id == scope.account_id,
                CapitalPolicyHeadRow.deployment_environment == scope.environment,
                CapitalPolicyHeadRow.symbol == scope.symbol,
            )
        )
    ).one_or_none()
    row: CapitalPolicyRevisionRow | None = None
    if head is not None:
        row = await _one(
            session,
            select(CapitalPolicyRevisionRow).where(CapitalPolicyRevisionRow.id == head[0]),
        )
    blocked = check_pointer(
        scope.account_id,
        scope.environment,
        scope.symbol,
        None if head is None else (head[0], head[1]),
        None
        if row is None
        else (
            row.id,
            row.exchange_account_id,
            row.deployment_environment,
            row.symbol,
            row.revision,
        ),
    )
    if blocked is not None:
        return blocked
    assert row is not None
    try:
        policy = parse_policy(row.schema_version, row.policy, row.digest)
    except PolicyRejectedError as exc:
        return Blocked(exc.reason, (("revision", str(row.id)),))
    return AppliedPolicy(
        scope.account_id, scope.environment, scope.symbol, row.revision, row.digest, row.id, policy
    )


def _context(
    now_ms: int, max_age: int, query: LedgerObservationQueryRow | None, block: Blocked | None
) -> CapitalReadContext:
    return CapitalReadContext(
        now_ms, max_age, _NO_QUERY if query is None else query.query_id, block
    )


async def read_capital(
    session: AsyncSession, scope: CapitalScope, *, now_ms: int, max_snapshot_age_ms: int
) -> LedgerCapitalRead:
    await _require_snapshot_transaction(session)
    account, environment = scope.account_id, scope.environment
    policy = await _policy(session, scope)

    query = await _one(
        session,
        select(LedgerObservationQueryRow)
        .where(
            LedgerObservationQueryRow.exchange_account_id == account,
            LedgerObservationQueryRow.deployment_environment == environment,
        )
        .order_by(LedgerObservationQueryRow.query_revision.desc()),
    )
    observation: LedgerObservationRow | None = None
    if query is not None:
        observation = await _one(
            session,
            select(LedgerObservationRow).where(LedgerObservationRow.query_id == query.query_id),
        )
    basis: AcceptedCapitalBasisRow | None = None
    clock = await session.scalar(
        select(CapitalCommandClockRow.revision).where(
            CapitalCommandClockRow.exchange_account_id == account,
            CapitalCommandClockRow.deployment_environment == environment,
        )
    )
    if observation is not None and observation.accepted:
        basis = await _one(
            session,
            select(AcceptedCapitalBasisRow).where(
                AcceptedCapitalBasisRow.observation_id == observation.id
            ),
        )
    if query is None or observation is None or basis is None:
        # The latest query has no accepted basis: an older basis is superseded.
        older = await session.scalar(
            select(AcceptedCapitalBasisRow.id)
            .where(
                AcceptedCapitalBasisRow.exchange_account_id == account,
                AcceptedCapitalBasisRow.deployment_environment == environment,
            )
            .limit(1)
        )
        pending = (
            Blocked("snapshot_query_pending", (("query", str(query.query_id)),))
            if older is not None and query is not None
            else None
        )
        result = derive_capital(
            scope=scope,
            accepted=None,
            attempts=(),
            uncertainties=(),
            policy=policy,
            read_context=_context(now_ms, max_snapshot_age_ms, query, pending),
        )
        return LedgerCapitalRead(result, None, None if query is None else query.query_id, clock)

    integrity: list[Blocked] = []
    if clock is None or basis.accept_revision > clock:
        integrity.append(
            Blocked(
                "snapshot_evidence_conflict",
                (("accept_revision", str(basis.accept_revision)), ("clock", str(clock))),
            )
        )

    symbol_row = await _one(
        session,
        select(AcceptedCapitalBasisSymbolRow).where(
            AcceptedCapitalBasisSymbolRow.basis_id == basis.id,
            AcceptedCapitalBasisSymbolRow.symbol == scope.symbol,
        ),
    )
    cells = await _all(
        session,
        select(AcceptedCapitalBasisCellRow).where(
            AcceptedCapitalBasisCellRow.basis_id == basis.id,
            AcceptedCapitalBasisCellRow.symbol == scope.symbol,
        ),
    )
    classified = await _all(
        session,
        select(AcceptedCapitalBasisAttemptRow).where(
            AcceptedCapitalBasisAttemptRow.basis_id == basis.id
        ),
    )
    listed = await _all(
        session,
        select(QuarantineOpeningRow)
        .join(
            AcceptedCapitalBasisQuarantineRow,
            AcceptedCapitalBasisQuarantineRow.quarantine_id == QuarantineOpeningRow.quarantine_id,
        )
        .where(AcceptedCapitalBasisQuarantineRow.basis_id == basis.id),
    )
    later = await _all(
        session,
        select(QuarantineOpeningRow).where(
            QuarantineOpeningRow.exchange_account_id == account,
            QuarantineOpeningRow.deployment_environment == environment,
            QuarantineOpeningRow.opened_revision > basis.accept_revision,
        ),
    )
    tail = await tail_attempts(
        session, account, environment, basis.attempt_seq_high_water, limit=MAX_TAIL_ATTEMPTS + 1
    )
    if len(tail) > MAX_TAIL_ATTEMPTS:
        integrity.append(
            Blocked("attempt_tail_unbounded", (("max_tail_attempts", str(MAX_TAIL_ATTEMPTS)),))
        )
        tail = tail[:MAX_TAIL_ATTEMPTS]

    unresolved = [row for row in classified if row.classification == "unresolved"]
    watched = [row.attempt_id for row in unresolved] + [row.attempt_id for row in tail]
    outcomes, resolutions = await attempt_evidence(session, watched)

    # Current open uncertainty: an UNKNOWN without resolution, or any quarantine
    # the basis has not seen (it keeps blocking until a new basis lists it).
    uncertainties: list[UncertaintyFact] = [
        UncertaintyFact(attempt_id, account, environment, symbol, True)
        for attempt_id, symbol in open_unknowns(
            [(row.attempt_id, row.symbol) for row in unresolved]
            + [(row.attempt_id, row.symbol) for row in tail],
            outcomes,
            resolutions,
        )
    ]
    seen = {row.quarantine_id for row in listed}
    uncertainties.extend(
        UncertaintyFact(row.quarantine_id, account, environment, row.symbol, True)
        for row in later
        if row.quarantine_id not in seen
    )

    attempts: list[AttemptFact] = []
    for row in tail:
        amount = _amount(row.normalized_payload)
        kind = outcomes.get(row.attempt_id)
        action = resolutions.get(row.attempt_id)
        if amount is None or (action is not None and kind != "unknown"):
            integrity.append(
                Blocked("attempt_evidence_conflict", (("attempt", str(row.attempt_id)),))
            )
            continue
        attempts.append(
            AttemptFact(
                row.attempt_id,
                CapitalScope(
                    row.exchange_account_id, row.deployment_environment, row.symbol, row.cell_id
                ),
                row.attempt_seq,
                amount,
                "pending" if kind is None else _OUTCOMES[kind],
                None if action is None else _RESOLUTIONS.get(action),
            )
        )

    symbols: tuple[SymbolCapital, ...] = ()
    if symbol_row is not None:
        symbols = (
            SymbolCapital(
                symbol_row.symbol,
                symbol_row.available,
                symbol_row.offered,
                symbol_row.credits,
                symbol_row.unattributed_credits,
                symbol_row.foreign_offers,
                tuple(sorted((cell.cell_id, cell.amount) for cell in cells)),
                _blocked(symbol_row.block),
            ),
        )
    accepted = AcceptedCapitalBasis(
        account,
        environment,
        observation.id,
        query.query_id,
        basis.attempt_seq_high_water,
        query.started_at_ms,
        observation.query_finished_at_ms,
        symbols,
        _ids(classified, "reflected"),
        _ids(classified, "settled"),
        tuple(sorted((row.attempt_id, row.symbol) for row in unresolved)),
        tuple(sorted((row.quarantine_id, row.symbol) for row in listed)),
        _blocked(basis.scope_block),
        quarantined_attempts=_ids(classified, "quarantined"),
    )
    result = derive_capital(
        scope=scope,
        accepted=accepted,
        attempts=tuple(attempts),
        uncertainties=tuple(uncertainties),
        policy=policy,
        read_context=_context(
            now_ms, max_snapshot_age_ms, query, integrity[0] if integrity else None
        ),
    )
    return LedgerCapitalRead(result, basis.id, query.query_id, clock)


def _ids(rows: list[AcceptedCapitalBasisAttemptRow], classification: str) -> frozenset[UUID]:
    return frozenset(row.attempt_id for row in rows if row.classification == classification)


__all__ = ["MAX_TAIL_ATTEMPTS", "read_capital"]
