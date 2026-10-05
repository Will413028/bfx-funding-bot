"""The cutover comparison's ``ledger_reader`` arm: legacy (F3 (i')) vs the ledger capital reader.

Both arms run in the comparison's one REPEATABLE READ READ ONLY transaction under the cutover
reader role, at the same as-of ``now_ms``:

* legacy: the runner's REST observation accepted read-only
  (``execution.capital_observed_baseline``);
* ledger: ``LedgerCapitalReader.read_capital`` over the latest accepted basis (the runner's
  cutover observation) and its tail.

Field map (``COMPARED_FIELDS``) is the fold arm's result field set minus what is not comparable
across the two sources:

* ``query_id``: the legacy arm stores nothing, so it has no capital query; the ledger's query is
  a ``ledger_observation_query`` row (different identity space);
* ledger-only: the observation id and the accepted basis (``CapitalView.observation_id`` /
  ``accepted``), like the fold arm treats them as typed carriers only;
* the legacy command fence vs the ledger ``attempt_seq_high_water``: an ``event_log`` sequence
  vs the seeded attempt sequence (the fold arm compares them because both come from legacy);
* a block's evidence: legacy blocks carry a reason only.

Declared divergence (F7, Will 2026-10-05): a credit group the seed basis holds as
``recent_fill`` stays multi-cell under the ledger (carried with the same cells), while legacy may
attribute the same credits exactly from ``funding_trades`` synced after its final snapshot. Such
a scope is ``declared``, not ``different``, only when re-evaluating the legacy budget with the
declared groups' extra cell exposure (and the unattributed amount they move) reproduces every
compared ledger field; the evidence lists each group. Anything else stays ``different``.
Halt-fill attribution (critique §2): the S1-4d "fill during the halt" case attributes the fill to
the same cell on both arms (legacy ``recent_fill`` to its offer's cell, ledger ``trade``), so no
compared field differs and nothing else is declared.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.auth_rest import LOAN_ID_PREFIX
from bfx_funding_bot.modules.execution.capital_observed_baseline import (
    CutoverObservation,
    ObservedAvailable,
    ObservedResult,
    ObservedScope,
    read_observed_baseline,
    require_read_only_snapshot,
)
from bfx_funding_bot.modules.execution.capital_shadow_port import (
    BaselineBlocked,
    BaselineNotComparable,
)
from bfx_funding_bot.modules.execution.event_store.entities import is_terminal_offer_status
from bfx_funding_bot.modules.ledger import LedgerCapitalReader
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisCreditCellRow,
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisRow,
    CapitalPolicyHeadRow,
    LedgerObservationCreditRow,
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    LedgerObservationWalletRow,
)
from bfx_funding_bot.modules.trading import (
    Available,
    Blocked,
    CapitalBudget,
    CapitalPolicy,
    CapitalResult,
    CapitalScope,
    CapitalSnapshot,
    evaluate_capital,
)
from bfx_funding_bot.modules.trading_shadow import canonical_bytes

ARM: Final = "ledger_reader"
COMPARED_FIELDS: Final = (
    "kind",
    "reason",
    "applied.account_id",
    "applied.environment",
    "applied.symbol",
    "applied.revision",
    "applied.digest",
    "applied.revision_id",
    "applied.policy",
    "snapshot.available_amount",
    "snapshot.unreflected_commitments",
    "snapshot.total_capital",
    "snapshot.cell_exposure",
    "budget.spendable",
    "budget.cell_limit",
    "budget.cell_headroom",
    "budget.max_new_offer",
    "budget.reason",
    "unattributed_credit_exposure",
)
# Why each fold-arm field or ledger-only value is left out (pinned by tests).
EXCLUDED_FIELDS: Final = {
    "query_id": "legacy arm stores no capital query; ledger query ids are another identity space",
    "observation_id": "ledger-only",
    "accepted": "ledger-only (the accepted basis)",
    "attribution.command_fence": "event_log fence vs seeded attempt_seq_high_water",
    "evidence": "legacy blocks carry no evidence",
}
DECLARED_SEED_BASIS: Final = "recent_fill"
# F7 covers only legacy's exact upgrade from synced funding trades (``traded >= live``,
# ``capital_repository._attribute_credits`` step 1). ``funding_trade_partial`` keeps the carried
# cells (never a strict subset) and any other basis is not an upgrade, so neither is declared.
F7_LEGACY_BASES: Final = frozenset(("funding_trade",))
_CARRIED: Final = frozenset(("carry", "recent_fill"))

type ArmStatus = Literal["equal", "declared", "different", "not_comparable", "error"]


@dataclass(frozen=True, slots=True)
class ArmResult:
    arm: str
    scope: CapitalScope
    status: ArmStatus
    legacy: Mapping[str, object] | None = None
    ledger: Mapping[str, object] | None = None
    differences: tuple[Mapping[str, object], ...] = ()
    declared: tuple[Mapping[str, object], ...] = ()
    reason: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CreditGroup:
    symbol: str
    amount: Decimal
    period_days: int | None
    mts_opening: int | None
    attribution_basis: str
    cells: frozenset[str]


def _view_fields(
    applied: tuple[UUID, str, str, int, str, UUID, CapitalPolicy], snapshot: CapitalSnapshot,
    budget: CapitalBudget, unattributed: Decimal,
) -> dict[str, object]:
    fields: dict[str, object] = {"kind": "available"}
    for name, value in zip(
        ("account_id", "environment", "symbol", "revision", "digest", "revision_id", "policy"),
        applied, strict=True,
    ):
        fields[f"applied.{name}"] = value
    for name in ("available_amount", "unreflected_commitments", "total_capital", "cell_exposure"):
        fields[f"snapshot.{name}"] = getattr(snapshot, name)
    for name in ("spendable", "cell_limit", "cell_headroom", "max_new_offer", "reason"):
        fields[f"budget.{name}"] = getattr(budget, name)
    fields["unattributed_credit_exposure"] = unattributed
    return fields


def ledger_fields(result: CapitalResult) -> dict[str, object]:
    if isinstance(result, Blocked):
        return {"kind": "blocked", "reason": result.reason}
    view = result.view
    applied = view.applied
    return _view_fields(
        (applied.account_id, applied.environment, applied.symbol, applied.revision,
         applied.digest, applied.revision_id, applied.policy),
        view.snapshot, view.budget, view.unattributed_credit_exposure,
    )


def legacy_fields(result: ObservedAvailable | BaselineBlocked) -> dict[str, object]:
    if isinstance(result, BaselineBlocked):
        return {"kind": "blocked", "reason": result.reason}
    return _view_fields(
        (result.account_id, result.environment, result.symbol, result.revision, result.digest,
         result.revision_id, result.policy),
        result.snapshot, result.budget, result.unattributed_credit_exposure,
    )


def _json(value: object) -> object:
    return json.loads(canonical_bytes(value))


def differences(ledger: Mapping[str, object], legacy: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    unknown = (ledger.keys() | legacy.keys()) - set(COMPARED_FIELDS)
    if unknown:
        raise ValueError(f"uncompared fields {sorted(unknown)}")
    return tuple(
        {"path": path, "ledger": _json(ledger.get(path)), "legacy": _json(legacy.get(path))}
        for path in COMPARED_FIELDS
        if ledger.get(path) != legacy.get(path)
    )


def legacy_credit_key(credit_id: str) -> tuple[str, str] | None:
    """(source kind, venue id) of a legacy credit id: loans carry ``LOAN_ID_PREFIX``.

    Stated here, not imported from the seed: the comparison checks the seed independently,
    and nothing deployed may import the seed (``test_ledger_seed_dormant``).
    """
    if credit_id.startswith(LOAN_ID_PREFIX):
        venue_id = credit_id.removeprefix(LOAN_ID_PREFIX)
        return ("loan", venue_id) if venue_id else None
    return ("credit", credit_id) if credit_id else None


def legacy_groups(credit_cells: Mapping[str, Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Legacy credit groups keyed like the ledger's; an unkeyable id explains nothing."""
    keyed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for credit_id, entry in credit_cells.items():
        key = legacy_credit_key(credit_id)
        if key is not None:
            keyed[key] = entry
    return keyed


async def basis_groups(session: AsyncSession, basis_id: UUID) -> dict[tuple[str, str], CreditGroup]:
    """A basis's credit groups and their exact cell sets (named columns only)."""
    rows = await session.execute(select(
        AcceptedCapitalBasisCreditRow.source_kind, AcceptedCapitalBasisCreditRow.venue_credit_id,
        AcceptedCapitalBasisCreditRow.symbol, AcceptedCapitalBasisCreditRow.amount,
        AcceptedCapitalBasisCreditRow.period_days, AcceptedCapitalBasisCreditRow.mts_opening,
        AcceptedCapitalBasisCreditRow.attribution_basis,
    ).where(AcceptedCapitalBasisCreditRow.basis_id == basis_id))
    cells: dict[tuple[str, str], set[str]] = {}
    for kind, venue_id, cell in await session.execute(select(
        AcceptedCapitalBasisCreditCellRow.source_kind,
        AcceptedCapitalBasisCreditCellRow.venue_credit_id,
        AcceptedCapitalBasisCreditCellRow.cell_id,
    ).where(AcceptedCapitalBasisCreditCellRow.basis_id == basis_id)):
        cells.setdefault((kind, venue_id), set()).add(cell)
    return {
        (kind, venue_id): CreditGroup(
            symbol, amount, period, opening, basis, frozenset(cells.get((kind, venue_id), ())),
        )
        for kind, venue_id, symbol, amount, period, opening, basis in rows.tuples()
    }


async def seed_basis_id(session: AsyncSession, account_id: UUID, environment: str) -> UUID | None:
    """The scope's seed basis: the basis of its one ``legacy_seed`` observation."""
    found = list(await session.scalars(
        select(AcceptedCapitalBasisRow.id)
        .join(LedgerObservationRow, LedgerObservationRow.id == AcceptedCapitalBasisRow.observation_id)
        .where(
            LedgerObservationRow.exchange_account_id == account_id,
            LedgerObservationRow.deployment_environment == environment,
            LedgerObservationRow.origin == "legacy_seed",
        )
    ))
    return found[0] if len(found) == 1 else None


def declare_divergence(
    scope: CapitalScope, legacy: ObservedAvailable, ledger: Mapping[str, object],
    ledger_groups: Mapping[tuple[str, str], CreditGroup],
    seed_groups: Mapping[tuple[str, str], CreditGroup],
) -> tuple[dict[str, object], ...] | None:
    """The F7 groups that explain every difference of this scope, or None.

    A group qualifies when the seed basis holds its (symbol, period, opening) as
    ``recent_fill``, the ledger still holds it carried with exactly the seed's cells, and
    legacy, by its exact funding-trade upgrade (``F7_LEGACY_BASES``), names a strict subset of
    those cells for the same credit and amount. Their extra
    exposure (and the unattributed amount they move) is added to the legacy answer, whose
    budget is re-evaluated with legacy's own policy; only an exact match declares.
    """
    seeded = {
        (g.symbol, g.period_days, g.mts_opening): g.cells
        for g in seed_groups.values() if g.attribution_basis == DECLARED_SEED_BASIS
    }
    extra_cell, moved_unattributed = Decimal(0), Decimal(0)
    evidence: list[dict[str, object]] = []
    legacy_by_key = legacy_groups(legacy.credit_cells)
    for (kind, venue_id), group in sorted(ledger_groups.items()):
        seed_cells = seeded.get((group.symbol, group.period_days, group.mts_opening))
        if (group.symbol != scope.symbol or seed_cells is None
                or group.attribution_basis not in _CARRIED or group.cells != seed_cells):
            continue
        entry = legacy_by_key.get((kind, venue_id))
        if entry is None or entry.get("basis") not in F7_LEGACY_BASES:
            continue
        legacy_cells = frozenset(str(cell) for cell in entry.get("cells") or ())
        if not legacy_cells < group.cells or Decimal(str(entry.get("amount"))) != group.amount:
            continue
        if scope.cell_id in group.cells and scope.cell_id not in legacy_cells:
            extra_cell += group.amount
        if not legacy_cells:
            moved_unattributed += group.amount
        evidence.append({
            "credit": f"{kind}:{venue_id}", "symbol": group.symbol, "amount": format(group.amount, "f"),
            "period_days": group.period_days, "mts_opening": group.mts_opening,
            "seed_basis": DECLARED_SEED_BASIS, "ledger_basis": group.attribution_basis,
            "ledger_cells": sorted(group.cells), "legacy_cells": sorted(legacy_cells),
            "legacy_basis": entry.get("basis"),
        })
    if not evidence:
        return None
    snapshot = CapitalSnapshot(
        legacy.snapshot.available_amount, legacy.snapshot.unreflected_commitments,
        legacy.snapshot.total_capital, legacy.snapshot.cell_exposure + extra_cell,
    )
    adjusted = legacy_fields(ObservedAvailable(
        legacy.account_id, legacy.environment, legacy.symbol, legacy.revision, legacy.digest,
        legacy.revision_id, legacy.policy, legacy.command_fence, snapshot,
        evaluate_capital(legacy.policy, snapshot),
        legacy.unattributed_credit_exposure - moved_unattributed, legacy.credit_cells,
        legacy.projection_cursor, legacy.watermark,
    ))
    if differences(ledger, adjusted):
        return None
    return tuple(evidence)


def _nonzero(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    return {key: value for key, value in values.items() if value != 0}


# Every observation field legacy's acceptance and classification read (``CapitalRepository.
# _validate_snapshot`` / ``_observation`` / ``_classify`` / ``_attribute_credits`` /
# ``_check_historical_intent``), by attribute name, and what ``bind_observation`` does with it.
# ``test_capital_comparison_cutover`` collects those attribute names from the source: a field
# legacy starts reading without an entry here fails that test.
BINDING_OF_LEGACY_FIELDS: Final[dict[str, str]] = {
    # snapshot
    "account_id": "compared: entry scope = ledger observation scope",
    "environment": "compared: entry scope = ledger observation scope",
    "query_started_at_ms": "compared: = ledger query started_at_ms",
    "query_finished_at_ms": "compared: = ledger query_finished_at_ms (confirmation: confirmation_finished_at_ms)",
    "wallet_available": "compared: = ledger funding wallets' available (non-zero)",
    "offers": "compared: row set = ledger_observation_offer",
    "credits": "compared: row set = ledger_observation_credit",
    "offer_history": "compared: row set = ledger_observation_offer_history",
    "coverage": "compared: the flags and window below",
    "schema_version": "not an observation value (stored event rows' schema check)",
    # coverage
    "active_offers_complete": "compared: = offers_complete",
    "active_credits_complete": "compared: = credits_complete and loans_complete",
    "wallets_complete": "compared: = wallets_complete",
    "offer_history_complete": "compared: = offer_history_complete",
    "offer_history_start_ms": "compared: = history_requested_start_ms",
    "offer_history_end_ms": "compared: = history_requested_end_ms",
    # offers, history rows and credits
    "venue_offer_id": "compared: offer and history row identity",
    "symbol": "compared: offers, history rows, credits",
    "status": "compared: offers, credits; history rows as legacy-terminal (ledger: terminal_kind)",
    "amount_original": "compared: offers, history rows",
    "amount_remaining": "compared: offers, history rows",
    "mts_created": "compared: offers, history rows, credits (recent_fill created <= opening)",
    "mts_updated": "compared: history rows (terminal within the history window)",
    "period_days": "compared: offers, credits (group key)",
    "credit_id": "compared: credit identity (source kind, venue id)",
    "amount": "compared: credits",
    "mts_opening": "compared: credits (group key)",
    # names shared with legacy rows read in the same methods, never observation values
    "cid": "not an observation value (claim/intent rows)",
    "execution_decision_id": "not an observation value (claim/attempt rows)",
    "signal_correlation_id": "not an observation value (claim rows)",
}
_LEGACY_NOT_TERMINAL: Final = frozenset(("absent", "quarantined"))


def _legacy_terminal(status: str) -> bool:
    """A history row legacy accepts as terminal evidence (``_classify`` / ``_attribute_credits``)."""
    return is_terminal_offer_status(status) and status not in _LEGACY_NOT_TERMINAL


async def bind_observation(session: AsyncSession, observation: CutoverObservation) -> list[dict[str, object]]:
    """Why the observation file is not the runner's accepted ledger observation (empty: bound).

    F3 (i') holds only if both arms read the same REST responses. The file names the runner's
    ledger observation; that observation must be the scope's latest query, venue-origin and
    accepted (never the seed's), carry the file's digests and query times, and hold exactly
    what legacy reads of the file's first observation (``BINDING_OF_LEGACY_FIELDS``): live
    offers, live credits, terminal offer history, coverage flags and history window, funding
    wallets.
    """
    ref, event = observation.ledger, observation.event
    account, environment = observation.account_id, observation.environment
    problems: list[dict[str, object]] = []

    def problem(reason: str, **evidence: object) -> None:
        problems.append({"reason": reason, **{k: _json(v) for k, v in sorted(evidence.items())}})

    def rows_differ(reason: str, file: set[Any], ledger: set[Any]) -> None:
        if file != ledger:
            problem(reason, file_only=sorted(map(str, file - ledger)),
                    ledger_only=sorted(map(str, ledger - file)))

    latest = (await session.execute(
        select(LedgerObservationQueryRow.query_id, LedgerObservationQueryRow.started_at_ms)
        .where(LedgerObservationQueryRow.exchange_account_id == account,
               LedgerObservationQueryRow.deployment_environment == environment)
        .order_by(LedgerObservationQueryRow.query_revision.desc()).limit(1))).first()
    if latest is None or latest.query_id != ref.query_id:
        problem("observation_not_latest_query", file=ref.query_id,
                latest=None if latest is None else latest.query_id)
    row = (await session.execute(select(
        LedgerObservationRow.query_id, LedgerObservationRow.exchange_account_id,
        LedgerObservationRow.deployment_environment, LedgerObservationRow.origin,
        LedgerObservationRow.accepted, LedgerObservationRow.query_finished_at_ms,
        LedgerObservationRow.confirmation_finished_at_ms, LedgerObservationRow.first_digest,
        LedgerObservationRow.confirmation_digest, LedgerObservationRow.offers_complete,
        LedgerObservationRow.credits_complete, LedgerObservationRow.loans_complete,
        LedgerObservationRow.wallets_complete, LedgerObservationRow.offer_history_complete,
        LedgerObservationRow.history_requested_start_ms,
        LedgerObservationRow.history_requested_end_ms,
    ).where(LedgerObservationRow.id == ref.observation_id))).first()
    if row is None:
        problem("observation_missing", observation_id=ref.observation_id)
        return problems
    if (row.query_id, row.exchange_account_id, row.deployment_environment) != (
        ref.query_id, account, environment,
    ):
        problem("observation_identity_mismatch", observation_id=ref.observation_id)
    if row.origin != "venue" or not row.accepted:
        problem("observation_not_accepted_venue", origin=row.origin, accepted=row.accepted)
    if (row.first_digest, row.confirmation_digest) != (ref.first_digest, ref.confirmation_digest):
        problem("observation_digest_mismatch")
    started = await session.scalar(select(LedgerObservationQueryRow.started_at_ms).where(
        LedgerObservationQueryRow.query_id == row.query_id))
    times = (started, row.query_finished_at_ms, row.confirmation_finished_at_ms)
    expected = (event.query_started_at_ms, event.query_finished_at_ms,
                observation.confirmation.query_finished_at_ms)
    if times != expected:
        problem("observation_times_mismatch", ledger=list(times), file=list(expected))
    coverage = event.coverage
    file_coverage = (
        coverage.active_offers_complete, coverage.active_credits_complete,
        coverage.active_credits_complete, coverage.wallets_complete,
        coverage.offer_history_complete, coverage.offer_history_start_ms,
        coverage.offer_history_end_ms,
    )
    ledger_coverage = (
        row.offers_complete, row.credits_complete, row.loans_complete, row.wallets_complete,
        row.offer_history_complete, row.history_requested_start_ms, row.history_requested_end_ms,
    )
    if file_coverage != ledger_coverage:
        problem("observation_coverage_differs", file=list(file_coverage),
                ledger=list(ledger_coverage))
    observed_id = ref.observation_id
    rows_differ("observation_offers_differ", {
        (o.venue_offer_id, o.symbol, o.status, o.amount_original, o.amount_remaining,
         o.period_days, o.mts_created) for o in event.offers
    }, set((await session.execute(select(
        LedgerObservationOfferRow.venue_offer_id, LedgerObservationOfferRow.symbol,
        LedgerObservationOfferRow.status, LedgerObservationOfferRow.amount_original,
        LedgerObservationOfferRow.amount_remaining, LedgerObservationOfferRow.period_days,
        LedgerObservationOfferRow.mts_created,
    ).where(LedgerObservationOfferRow.observation_id == observed_id))).tuples().all()))
    # Legacy reads a history row's status only as "terminal evidence or not"
    # (``_legacy_terminal``). A ledger history row keeps the offer's last live status and states
    # its terminality in ``terminal_kind`` (executed / canceled), so that is what goes through
    # legacy's rule on the ledger side.
    rows_differ("observation_offer_history_differs", {
        (o.venue_offer_id, o.symbol, o.amount_original, o.amount_remaining, o.mts_created,
         o.mts_updated, _legacy_terminal(o.status)) for o in event.offer_history
    }, {
        (venue_id, symbol, original, remaining, created, updated, _legacy_terminal(kind))
        for venue_id, symbol, original, remaining, created, updated, kind in (
            await session.execute(select(
                LedgerObservationOfferHistoryRow.venue_offer_id,
                LedgerObservationOfferHistoryRow.symbol,
                LedgerObservationOfferHistoryRow.amount_original,
                LedgerObservationOfferHistoryRow.amount_remaining,
                LedgerObservationOfferHistoryRow.mts_created,
                LedgerObservationOfferHistoryRow.mts_updated,
                LedgerObservationOfferHistoryRow.terminal_kind,
            ).where(LedgerObservationOfferHistoryRow.observation_id == observed_id))).tuples()
    })
    rows_differ("observation_credits_differ", {
        (legacy_credit_key(c.credit_id), c.symbol, c.status, c.amount, c.period_days,
         c.mts_opening, c.mts_created) for c in event.credits
    }, {
        ((kind, venue_id), symbol, status, amount, period, opening, created)
        for kind, venue_id, symbol, status, amount, period, opening, created in (
            await session.execute(select(
                LedgerObservationCreditRow.source_kind, LedgerObservationCreditRow.venue_credit_id,
                LedgerObservationCreditRow.symbol, LedgerObservationCreditRow.status,
                LedgerObservationCreditRow.amount, LedgerObservationCreditRow.period_days,
                LedgerObservationCreditRow.mts_opening, LedgerObservationCreditRow.mts_created,
            ).where(LedgerObservationCreditRow.observation_id == observed_id))).tuples()
    })
    wallets: dict[str, Decimal] = {str(symbol): amount for symbol, amount in (await session.execute(select(
        LedgerObservationWalletRow.symbol, LedgerObservationWalletRow.available,
    ).where(LedgerObservationWalletRow.observation_id == observed_id,
            LedgerObservationWalletRow.wallet_type == "funding",
            LedgerObservationWalletRow.symbol.is_not(None)))).tuples().all()}
    if _nonzero(dict(event.wallet_available)) != _nonzero(wallets):
        problem("observation_wallets_differ")
    return problems


async def compare_ledger_arm(
    session: AsyncSession, *, scope: CapitalScope, evaluated: ObservedScope,
    ledger_reader: LedgerCapitalReader, now_ms: int, max_snapshot_age_ms: int,
) -> ArmResult:
    """One scope: legacy (i') vs the ledger reader, at the same ``now_ms``.

    A listed cell whose symbol has no policy head is ``not_comparable`` (both arms would only
    agree that it is blocked), as in the fold arm.
    """
    try:
        await require_read_only_snapshot(session)
        head = (await session.execute(select(CapitalPolicyHeadRow.revision).where(
            CapitalPolicyHeadRow.exchange_account_id == scope.account_id,
            CapitalPolicyHeadRow.deployment_environment == scope.environment,
            CapitalPolicyHeadRow.symbol == scope.symbol,
        ))).first()
        if head is None:
            return ArmResult(ARM, scope, "not_comparable", reason="policy_missing", evidence={
                "table": "capital_policy_heads", "symbol": scope.symbol,
            })
        unbound = await bind_observation(session, evaluated.observation)
        if unbound:
            return ArmResult(ARM, scope, "not_comparable", reason="observation_binding_mismatch",
                             evidence={"problems": unbound})
        legacy: ObservedResult = await read_observed_baseline(
            session, evaluated, scope=scope, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms,
        )
        read = await ledger_reader.read_capital(
            session, scope, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms,
        )
    except Exception as exc:
        return ArmResult(ARM, scope, "error", reason=type(exc).__name__)
    if read.query_id != evaluated.observation.ledger.query_id:
        return ArmResult(ARM, scope, "not_comparable", reason="observation_binding_mismatch",
                         evidence={"problems": [{"reason": "ledger_read_on_another_query",
                                                 "read": _json(read.query_id)}]})
    if isinstance(legacy, BaselineNotComparable):
        return ArmResult(ARM, scope, "not_comparable", reason=legacy.reason, evidence={
            "projection_cursor": legacy.projection_cursor, "watermark": legacy.watermark,
        })
    left, right = ledger_fields(read.result), legacy_fields(legacy)
    evidence: dict[str, object] = {
        "ledger_basis_id": None if read.basis_id is None else str(read.basis_id),
        "ledger_query_id": None if read.query_id is None else str(read.query_id),
        "legacy_command_fence": legacy.command_fence if isinstance(legacy, ObservedAvailable) else None,
        "legacy_watermark": legacy.watermark,
    }
    found = differences(left, right)
    if not found:
        return ArmResult(ARM, scope, "equal", right, left, evidence=evidence)
    if isinstance(legacy, ObservedAvailable) and isinstance(read.result, Available) and read.basis_id:
        try:
            seed = await seed_basis_id(session, scope.account_id, scope.environment)
            declared = None if seed is None else declare_divergence(
                scope, legacy, left, await basis_groups(session, read.basis_id),
                await basis_groups(session, seed),
            )
        except Exception as exc:
            return ArmResult(ARM, scope, "error", right, left, found, reason=type(exc).__name__)
        if declared is not None:
            return ArmResult(ARM, scope, "declared", right, left, found, declared,
                             reason="f7_seeded_recent_fill_multi_cell", evidence=evidence)
    return ArmResult(ARM, scope, "different", right, left, found, evidence=evidence)


def summarize_arm(scopes: Sequence[CapitalScope], results: Sequence[ArmResult]) -> dict[str, Any]:
    """Per-arm summary; passes only with every scope ``equal`` or ``declared`` and conclusive."""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    blocked = [r for r in results if r.status == "equal" and (r.ledger or {}).get("kind") == "blocked"]
    inconclusive = not scopes or (len(blocked) == len(scopes) and all(
        isinstance(r.ledger.get("reason"), str) and (
            r.ledger["reason"] == "snapshot_stale" or "missing" in str(r.ledger["reason"]))
        for r in blocked if r.ledger is not None
    ))
    complete = len(scopes) == len(set(scopes)) == len(results) and [r.scope for r in results] == list(scopes)
    passed = complete and not inconclusive and counts.get("equal", 0) + counts.get("declared", 0) == len(scopes)
    return {
        "expected_scopes": len(scopes), "emitted_scopes": len(results),
        "coverage_complete": complete, "counts": dict(sorted(counts.items())),
        "blocked_equal": len(blocked), "inconclusive": inconclusive, "passed": passed,
    }


def arm_json(result: ArmResult) -> dict[str, Any]:
    return {
        "kind": "arm", "arm": result.arm, "scope": _json(result.scope), "status": result.status,
        "legacy": None if result.legacy is None else _json(dict(result.legacy)),
        "ledger": None if result.ledger is None else _json(dict(result.ledger)),
        "differences": list(result.differences), "declared": list(result.declared),
        "reason": result.reason, "evidence": _json(dict(result.evidence)),
    }


__all__ = [
    "ARM",
    "BINDING_OF_LEGACY_FIELDS",
    "COMPARED_FIELDS",
    "EXCLUDED_FIELDS",
    "ArmResult",
    "CreditGroup",
    "arm_json",
    "basis_groups",
    "bind_observation",
    "compare_ledger_arm",
    "declare_divergence",
    "differences",
    "ledger_fields",
    "legacy_credit_key",
    "legacy_fields",
    "legacy_groups",
    "seed_basis_id",
    "summarize_arm",
]
