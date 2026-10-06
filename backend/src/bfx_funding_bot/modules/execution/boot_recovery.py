"""The halt-2 cutover's PENDING -> UNKNOWN conversion over the frozen legacy event log.

The legacy boot and runtime reconcile (``BootRecovery``) is gone with the legacy runtime
(S1-8); the ledger's observation cycle replaced it. ``convert_pending_to_unknown`` stays
for the one reader that still replays the legacy stream with it: the baseline restore drill's
replay (``scripts/verify_projection_replay.py``, run by
``deploy/vm/pgbackrest/restore_commands.py``). It goes in D4b, when that drill verifies the
ledger instead.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.execution.events import ReservationUnknown
from bfx_funding_bot.modules.execution.registry_offers import RegistryState


class RecoveryCorrelationError(RuntimeError):
    """Recovery cannot safely emit a lifecycle event without audited identity."""


async def convert_pending_to_unknown(
    session: AsyncSession,
    *,
    account_id: str,
    environment: str,
    now_ms: int,
) -> int:
    """Durably turn every unresolved PENDING claim into UNKNOWN once.

    This is the explicit cutover counterpart of boot recovery's stale-PENDING
    branch.  It performs no venue or executor work: the serialized event
    writer appends ``ReservationUnknown`` and projects its uncertainty in the
    same account transaction.  A rerun sees UNKNOWN rather than PENDING, so it
    appends neither a second outcome nor a second uncertainty.
    """
    try:
        canonical = UUID(account_id)
    except ValueError as exc:
        raise ValueError("PENDING conversion requires a canonical account UUID") from exc
    pending = (
        await session.execute(
            select(OfferClaimRow).where(
                OfferClaimRow.exchange_account_id == canonical,
                OfferClaimRow.deployment_environment == environment,
                OfferClaimRow.state == RegistryState.PENDING.value,
            ).order_by(OfferClaimRow.last_event_seq.asc(), OfferClaimRow.cid.asc())
        )
    ).scalars().all()
    events: list[object] = []
    for claim in pending:
        if claim.execution_decision_id is None:
            raise RecoveryCorrelationError(
                f"unresolved PENDING cid={claim.cid} has no reservation decision id"
            )
        try:
            signal_id = UUID(str(claim.signal_correlation_id))
        except ValueError as exc:
            raise RecoveryCorrelationError(
                f"unresolved PENDING cid={claim.cid} has invalid signal correlation"
            ) from exc
        events.append(ReservationUnknown(
            symbol=claim.symbol,
            cid=claim.cid,
            signal_correlation_id=signal_id,
            account_id=str(canonical),
            is_simulated=False,
            reason="unresolved_at_halt2_cutover",
            amount=Decimal(str(claim.size_usdt)),
            occurred_at_ms=now_ms,
            reservation_ref=ReservationRef(
                execution_decision_id=claim.execution_decision_id,
                cid=claim.cid,
                signal_correlation_id=signal_id,
            ),
        ))
    if not events:
        return 0
    await AccountEventWriter(
        store=PostgresEventStore(deployment_environment=environment)
    ).append_batch(session, events)
    return len(events)


__all__ = ["RecoveryCorrelationError", "convert_pending_to_unknown"]
