"""Amount fingerprints: the identity a funding submit carries without a cid.

Bitfinex funding offers drop the client cid, so after an ambiguous submit the
only link between our durable attempt and a venue offer is what the offer
looks like. Lending envelope D3a makes that link sharp: every submitted amount
encodes a fingerprint in its last four of eight decimals (1..9999), unique
among the symbol's non-terminal claims and unresolved attempts. An offer with
the attempt's exact amount, rate and period created after the attempt started
is then that attempt's offer, not a coincidence -- which is what lets a later
complete snapshot resolve an UNKNOWN without an operator.

The amount moves down by less than 0.0001 and never leaves the bounds the
planner sized it within. The planner chooses the fingerprint; the command gate
re-checks uniqueness under the account lock, in the transaction that writes the
intent, so two submits can never share one. The chosen Decimal travels unchanged
through the decision, the durable intent/attempt and the venue body, so every
fingerprint in the space is usable.
"""
from __future__ import annotations

from collections.abc import Collection
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)

AMOUNT_QUANTUM = Decimal("0.00000001")
FINGERPRINT_STEP = Decimal("0.0001")
FINGERPRINT_SPACE = 9999  # values 1..9999; 0 means "no fingerprint"

# An UNKNOWN claim stays UNKNOWN for audit after its attempt is resolved as not
# sent; while it is unresolved its attempt holds the fingerprint (below).
_OPEN_CLAIM_STATES = (RegistryState.PENDING.value, RegistryState.CLAIMED.value)


def fingerprint_of(amount: object) -> int | None:
    """The fingerprint an amount carries, or None when it is not a valid wire amount."""
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= 0 or value != value.quantize(AMOUNT_QUANTUM):
        return None
    return int(value.scaleb(8)) % 10_000


def fingerprint_seed(key: str) -> int:
    """Deterministic starting fingerprint for a submit identity."""
    return int.from_bytes(sha256(key.encode()).digest()[:8], "big") % FINGERPRINT_SPACE + 1


def fingerprinted(planned: Decimal, fingerprint: int) -> Decimal:
    """``planned`` rounded down to 4 decimals, carrying ``fingerprint``, never above it."""
    if not 1 <= fingerprint <= FINGERPRINT_SPACE:
        raise ValueError("fingerprint out of range")
    base = planned.quantize(FINGERPRINT_STEP, rounding=ROUND_DOWN)
    amount = base + fingerprint * AMOUNT_QUANTUM
    if amount > planned:
        amount -= FINGERPRINT_STEP
    return amount


def choose_fingerprinted_amount(
    planned: Decimal, *, seed_key: str, in_use: Collection[int], minimum: Decimal,
    maximum: Decimal | None,
) -> Decimal | None:
    """The fingerprinted amount for one submit, or None when no fingerprint fits.

    Starts at the fingerprint derived from ``seed_key`` and probes forward past
    fingerprints already in use or amounts outside ``[minimum, maximum]``.
    """
    if not planned.is_finite() or planned <= 0:
        return None
    start = fingerprint_seed(seed_key)
    for step in range(FINGERPRINT_SPACE):
        fingerprint = (start - 1 + step) % FINGERPRINT_SPACE + 1
        if fingerprint in in_use:
            continue
        amount = fingerprinted(planned, fingerprint)
        if amount < minimum or amount <= 0 or (maximum is not None and amount > maximum):
            continue
        return amount
    return None


async def fingerprints_in_use(session: AsyncSession, *, account_id: UUID, environment: str,
                              symbol: str) -> frozenset[int]:
    """Fingerprints held by the symbol's non-terminal claims and unresolved attempts.

    An attempt stays unresolved while it has no outcome or its UNKNOWN is still
    open; one resolved as not sent keeps ``outcome_kind='unknown'`` for audit but
    frees its fingerprint.
    """
    claims = (await session.scalars(select(OfferClaimRow.size_usdt).where(
        OfferClaimRow.exchange_account_id == account_id,
        OfferClaimRow.deployment_environment == environment,
        OfferClaimRow.symbol == symbol,
        OfferClaimRow.state.in_(_OPEN_CLAIM_STATES),
    ))).all()
    open_unknown = select(ExecutionUncertaintyRow.attempt_id).where(
        ExecutionUncertaintyRow.exchange_account_id == account_id,
        ExecutionUncertaintyRow.deployment_environment == environment,
        ExecutionUncertaintyRow.symbol == symbol,
        ExecutionUncertaintyRow.kind == "submit_outcome_unknown",
        ExecutionUncertaintyRow.state == "open",
    )
    attempts = (await session.scalars(select(SubmissionAttemptRow.normalized_payload).where(
        SubmissionAttemptRow.exchange_account_id == account_id,
        SubmissionAttemptRow.deployment_environment == environment,
        SubmissionAttemptRow.symbol == symbol,
        (SubmissionAttemptRow.outcome_kind.is_(None))
        | ((SubmissionAttemptRow.outcome_kind == "unknown")
           & SubmissionAttemptRow.attempt_id.in_(open_unknown)),
    ))).all()
    held = [fingerprint_of(amount) for amount in claims]
    held += [fingerprint_of(payload.get("amount")) if isinstance(payload, dict) else None
             for payload in attempts]
    return frozenset(value for value in held if value)


__all__ = [
    "AMOUNT_QUANTUM",
    "FINGERPRINT_SPACE",
    "choose_fingerprinted_amount",
    "fingerprint_of",
    "fingerprint_seed",
    "fingerprinted",
    "fingerprints_in_use",
]
