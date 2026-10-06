"""The weekly's pre-S1-8 legacy offer -> cell read, and migration ``a0b1c2d3e4f5``'s copy on demand.

Deleted together with the legacy runtime (S1-8 PR-C), with the tests that use it: both read
the legacy tables and models that PR removes.

* ``reference_legacy_offer_cells``: the loader's read before ``a0b1c2d3e4f5``, verbatim
  (``offer_claims`` / ``venue_offer_state`` / ``ORDER_FILL`` links resolved through
  ``execution_decisions`` / ``diagnostics``), shaped as the loader's ``legacy_offer_cells``
  seam, so a test can compute the weekly from the legacy tables and compare.
* ``materialize``: the migration's own copy, run again after a test wrote legacy rows past
  head (an e2e legacy process). It refuses, as the migration does, unless the epoch is
  ``ledger``.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.accounts.exchange_accounts import account_scope_clause
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    VenueOfferStateRow,
)

REVISION = "a0b1c2d3e4f5"
PARENT = "f9a0b1c2d3e4"
_PATH = Path(__file__).resolve().parents[2] / "alembic/versions" / f"{REVISION}_attribution_legacy_links.py"


def migration() -> Any:
    spec = importlib.util.spec_from_file_location("attribution_legacy_links_migration", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def materialize(factory: async_sessionmaker[AsyncSession]) -> None:
    module = migration()
    async with factory.begin() as session:
        await session.run_sync(lambda s: module.materialize(s.connection()))


@dataclass(frozen=True)
class OfferLink:
    venue_offer_id: str
    execution_decision_id: str | None
    signal_correlation_id: str | None


def resolve_offer_cells(
    links: Iterable[OfferLink], *, cell_by_decision: dict[str, str], cell_by_scid: dict[str, str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for link in links:
        by_decision = cell_by_decision.get(link.execution_decision_id or "")
        cell = by_decision or cell_by_scid.get(link.signal_correlation_id or "")
        if cell and (by_decision or link.venue_offer_id not in out):
            out[link.venue_offer_id] = cell
    return out


async def reference_legacy_offer_cells(
    session: AsyncSession, account_uuid: UUID, env: str,
) -> dict[str, frozenset[str]]:
    account_id = str(account_uuid)

    def scoped(table: Any) -> Any:
        return account_scope_clause(
            session, account_id=account_id,
            exchange_account_column=table.exchange_account_id,
            legacy_account_column=table.account_id,
        )

    claims = (await session.execute(select(
        OfferClaimRow.venue_offer_id, OfferClaimRow.execution_decision_id,
        OfferClaimRow.signal_correlation_id,
    ).where(
        OfferClaimRow.exchange_account_id == account_uuid,
        OfferClaimRow.deployment_environment == env,
        OfferClaimRow.venue_offer_id.is_not(None),
    ))).all()
    venue_offers = (await session.execute(select(
        VenueOfferStateRow.venue_offer_id, VenueOfferStateRow.execution_decision_id,
        VenueOfferStateRow.signal_correlation_id,
    ).where(
        VenueOfferStateRow.exchange_account_id == account_uuid,
        VenueOfferStateRow.deployment_environment == env,
    ))).all()
    fills = (await session.scalars(select(EventLogRow).where(
        EventLogRow.event_type == "ORDER_FILL", scoped(EventLogRow),
        EventLogRow.deployment_environment == env,
    ))).all()
    decisions = (await session.execute(select(
        ExecutionDecisionRow.decision_id, ExecutionDecisionRow.signal_correlation_id,
        ExecutionDecisionRow.cell_id,
    ).where(
        scoped(ExecutionDecisionRow),
        ExecutionDecisionRow.deployment_environment == env,
    ))).all()
    diagnostics = (await session.scalars(select(DiagnosticsRow).where(
        DiagnosticsRow.kind == "decision", scoped(DiagnosticsRow),
        DiagnosticsRow.deployment_environment == env,
    ))).all()
    cell_by_scid = {
        str(d.payload.get("correlation_id")): str(d.payload.get("cell"))
        for d in diagnostics
        if d.payload.get("correlation_id") and d.payload.get("cell")
    }
    cell_by_scid.update({scid: cell for _id, scid, cell in decisions})
    links = [OfferLink(str(voi), edid, scid) for voi, edid, scid in claims]
    links += [OfferLink(voi, edid, scid) for voi, edid, scid in venue_offers]
    links += [OfferLink(
        str(f.payload.get("venue_offer_id") or f.venue_offer_id or ""), None,
        str(f.payload.get("signal_correlation_id") or "") or None,
    ) for f in fills]
    resolved = resolve_offer_cells(
        links, cell_by_decision={did: cell for did, _scid, cell in decisions},
        cell_by_scid=cell_by_scid,
    )
    return {offer: frozenset({cell}) for offer, cell in resolved.items()}
