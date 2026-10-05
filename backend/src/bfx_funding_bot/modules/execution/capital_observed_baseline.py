"""The cutover comparison's legacy arm: legacy capital of a REST observation, read only (F3 (i')).

The cutover runner (S1-5) takes two REST observations of each account and writes only the
ledger's cutover observation. The same responses, in legacy's own observation structure
(``VenueSnapshotObserved``, serialized with ``serialize_event`` exactly as ``event_log`` stores
it), feed this arm: ``CapitalRepository.evaluate_observation_read_only`` runs acceptance's
validation and classification (``_validate_snapshot`` + ``_classify`` + the historical-intent
proof) and ``read_observed`` runs the authorization read (``_read_capital``'s basis and tail
fold) over the in-memory classification. Nothing is written: no query row, no event, no prefix
link, no snapshot row, no lock. It runs in the comparison's REPEATABLE READ READ ONLY
transaction under the cutover reader role (column grants: ``alembic/versions/d7e8f9a0b1c2_*``).

Observation file (JSON object)::

    {"format": "bfx-cutover-observation", "version": 1,
     "observations": [{"account_id": "<uuid>", "environment": "prod|shadow|ci",
                       "event": <serialize_event(VenueSnapshotObserved)>,
                       "confirmation": <serialize_event(VenueSnapshotObserved)>,
                       "ledger": {"query_id": "<uuid>", "observation_id": "<uuid>",
                                  "first_digest": "<hex>", "confirmation_digest": "<hex>"}}]}

One entry per (account, environment); ``event`` carries the offer history, ``confirmation`` is
the second observation (legacy ``BootRecovery.run``'s shape). The acceptance products
(``capital_query_id``, ``capital_command_fence``, ``capital_confirmation``,
``capital_classification_digest``) must be absent: this arm derives them. ``ledger`` names the
cutover observation the runner wrote from the same responses; the comparison binds the entry
to it (``apps/capital_comparison_ledger.bind_observation``) before either arm counts.

Deleted with the legacy authority (S1-8).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.capital_repository import (
    AppliedCapitalPolicy,
    CapitalBlockedError,
    CapitalRepository,
    ObservedAcceptance,
    policy_from_row,
    read_policy_row,
)
from bfx_funding_bot.modules.execution.capital_shadow_baseline import projection_lag
from bfx_funding_bot.modules.execution.capital_shadow_port import (
    BaselineBlocked,
    BaselineNotComparable,
    CapitalScopeLike,
)
from bfx_funding_bot.modules.execution.event_store.serialization import deserialize_event
from bfx_funding_bot.modules.execution.events import VenueSnapshotObserved
from bfx_funding_bot.modules.trading import CapitalBudget, CapitalPolicy, CapitalSnapshot

OBSERVATION_FORMAT: Final = "bfx-cutover-observation"
OBSERVATION_VERSION: Final = 1
_ENVIRONMENTS: Final = frozenset(("prod", "shadow", "ci"))
_ACCEPTANCE_PRODUCTS: Final = (
    "capital_query_id",
    "capital_command_fence",
    "capital_confirmation",
    "capital_classification_digest",
)


class ObservationRejectedError(ValueError):
    """The observation file is not a usable runner observation; ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LedgerObservationRef:
    """The runner's ledger observation of the same REST responses (its identity and digests)."""

    query_id: UUID
    observation_id: UUID
    first_digest: str
    confirmation_digest: str


@dataclass(frozen=True, slots=True)
class CutoverObservation:
    account_id: UUID
    environment: str
    event: VenueSnapshotObserved
    confirmation: VenueSnapshotObserved
    ledger: LedgerObservationRef


@dataclass(frozen=True, slots=True)
class ObservedAvailable:
    """Legacy capital of one scope from the observation; no snapshot row, so no query id."""

    account_id: UUID
    environment: str
    symbol: str
    revision: int
    digest: str
    revision_id: UUID
    policy: CapitalPolicy
    command_fence: int
    snapshot: CapitalSnapshot
    budget: CapitalBudget
    unattributed_credit_exposure: Decimal
    # The classification's credit groups (``credit_id -> {symbol, amount, period, opening,
    # cells, basis}``): attribution evidence for the declared-divergence classifier.
    credit_cells: Mapping[str, Mapping[str, Any]]
    projection_cursor: int | None
    watermark: int
    status: Literal["available"] = "available"


type ObservedResult = ObservedAvailable | BaselineBlocked | BaselineNotComparable


def _event(value: object) -> VenueSnapshotObserved:
    if not isinstance(value, dict):
        raise ObservationRejectedError("observation_event_invalid")
    try:
        event = deserialize_event("VENUE_SNAPSHOT_OBSERVED", dict(value))
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise ObservationRejectedError("observation_event_invalid") from None
    if not isinstance(event, VenueSnapshotObserved):
        raise ObservationRejectedError("observation_event_invalid")
    if any(getattr(event, name) is not None for name in _ACCEPTANCE_PRODUCTS):
        raise ObservationRejectedError("observation_carries_acceptance")
    return event


def parse_observations(payload: object) -> tuple[CutoverObservation, ...]:
    """The runner observation file's entries, or ``ObservationRejectedError``."""
    if (not isinstance(payload, dict)
            or set(payload) != {"format", "version", "observations"}
            or payload["format"] != OBSERVATION_FORMAT
            or payload["version"] != OBSERVATION_VERSION
            or not isinstance(payload["observations"], list)
            or not payload["observations"]):
        raise ObservationRejectedError("observation_file_invalid")
    found: dict[tuple[UUID, str], CutoverObservation] = {}
    for item in payload["observations"]:
        if not isinstance(item, dict) or set(item) != {
            "account_id", "environment", "event", "confirmation", "ledger",
        }:
            raise ObservationRejectedError("observation_entry_invalid")
        ledger = item["ledger"]
        try:
            account = UUID(str(item["account_id"]))
            if not isinstance(ledger, dict) or set(ledger) != {
                "query_id", "observation_id", "first_digest", "confirmation_digest",
            } or not all(isinstance(ledger[k], str) and ledger[k]
                         for k in ("first_digest", "confirmation_digest")):
                raise ValueError
            ref = LedgerObservationRef(
                UUID(str(ledger["query_id"])), UUID(str(ledger["observation_id"])),
                ledger["first_digest"], ledger["confirmation_digest"],
            )
        except ValueError:
            raise ObservationRejectedError("observation_entry_invalid") from None
        environment = item["environment"]
        if environment not in _ENVIRONMENTS:
            raise ObservationRejectedError("observation_entry_invalid")
        event, confirmation = _event(item["event"]), _event(item["confirmation"])
        for observed in (event, confirmation):
            if (observed.account_id, observed.environment) != (str(account), environment):
                raise ObservationRejectedError("observation_scope_mismatch")
        if (account, environment) in found:
            raise ObservationRejectedError("observation_scope_duplicate")
        found[(account, environment)] = CutoverObservation(
            account, environment, event, confirmation, ref,
        )
    return tuple(found.values())


def window_start_ms(observations: Sequence[CutoverObservation]) -> int:
    """Where Q6's 300 s window begins: the earliest query start of any account's observations.

    Legacy classifies the first observation (``event``), so the window must cover its data,
    for the oldest account too (ADR Followup: freshness is never relaxed).
    """
    return min(
        min(o.event.query_started_at_ms, o.confirmation.query_started_at_ms)
        for o in observations
    )


def observed_as_of_ms(observations: Sequence[CutoverObservation]) -> int:
    """The instant every observation is complete: the latest query finish."""
    return max(
        max(o.event.query_finished_at_ms, o.confirmation.query_finished_at_ms)
        for o in observations
    )


async def require_read_only_snapshot(session: AsyncSession) -> None:
    """The arm only runs inside REPEATABLE READ READ ONLY (never a read-write transaction)."""
    row = (await session.execute(text(
        "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')"
    ))).one()
    if (row[0], row[1]) != ("repeatable read", "on"):
        raise ValueError("repeatable_read_read_only_required")


@dataclass(frozen=True, slots=True)
class ObservedScope:
    """One account's acceptance (or its refusal), evaluated once for all its cells."""

    observation: CutoverObservation
    acceptance: ObservedAcceptance | None
    refusal: str | None
    projection_cursor: int | None
    watermark: int
    lag: BaselineNotComparable | None


async def evaluate_scope(
    session: AsyncSession, observation: CutoverObservation, *, now_ms: int,
    max_snapshot_age_ms: int,
) -> ObservedScope:
    """Acceptance of ``observation`` in the caller's read-only snapshot (no write)."""
    await require_read_only_snapshot(session)
    repo = CapitalRepository(
        account_id=observation.account_id, environment=observation.environment,
        max_snapshot_age_ms=max_snapshot_age_ms,
    )
    with session.no_autoflush:
        cursor, watermark, lag = await projection_lag(
            session, account_id=observation.account_id, environment=observation.environment,
        )
        if lag is not None:
            return ObservedScope(observation, None, None, cursor, watermark, lag)
        try:
            acceptance = await repo.evaluate_observation_read_only(
                session, event=observation.event, confirmation=observation.confirmation,
                now_ms=now_ms,
            )
        except CapitalBlockedError as exc:
            return ObservedScope(observation, None, str(exc), cursor, watermark, None)
    return ObservedScope(observation, acceptance, None, cursor, watermark, None)


async def read_observed_baseline(
    session: AsyncSession, evaluated: ObservedScope, *, scope: CapitalScopeLike, now_ms: int,
    max_snapshot_age_ms: int,
) -> ObservedResult:
    """One cell's legacy capital: policy first (as ``read_capital``), then the read."""
    await require_read_only_snapshot(session)
    if (scope.account_id, scope.environment) != (
        evaluated.observation.account_id, evaluated.observation.environment,
    ):
        raise ValueError("observed_scope_mismatch")
    if evaluated.lag is not None:
        return evaluated.lag
    cursor, watermark = evaluated.projection_cursor, evaluated.watermark
    repo = CapitalRepository(
        account_id=scope.account_id, environment=scope.environment,
        max_snapshot_age_ms=max_snapshot_age_ms,
    )
    with session.no_autoflush:
        try:
            row = await read_policy_row(
                session, account_id=scope.account_id, environment=scope.environment,
                symbol=scope.symbol,
            )
            applied = AppliedCapitalPolicy(row.revision, row.digest, policy_from_row(row), row.id)
            if evaluated.acceptance is None:
                assert evaluated.refusal is not None
                raise CapitalBlockedError(evaluated.refusal)
            view = await repo.read_observed(
                session, evaluated.acceptance, symbol=scope.symbol, cell_id=scope.cell_id,
                now_ms=now_ms, applied=applied,
            )
        except CapitalBlockedError as exc:
            return BaselineBlocked(str(exc), cursor, watermark)
    return ObservedAvailable(
        scope.account_id, scope.environment, scope.symbol, applied.revision, applied.digest,
        applied.revision_id, applied.policy, view.command_fence, view.snapshot, view.budget,
        view.unattributed_credit_exposure, dict(view.attribution.get("credit_cells") or {}),
        cursor, watermark,
    )


__all__ = [
    "OBSERVATION_FORMAT",
    "OBSERVATION_VERSION",
    "CutoverObservation",
    "LedgerObservationRef",
    "ObservationRejectedError",
    "ObservedAvailable",
    "ObservedResult",
    "ObservedScope",
    "evaluate_scope",
    "observed_as_of_ms",
    "parse_observations",
    "read_observed_baseline",
    "require_read_only_snapshot",
    "window_start_ms",
]
