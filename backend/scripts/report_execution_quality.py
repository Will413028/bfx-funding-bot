"""Operator report — submit→first-fill latency + fill rate per config regime.

Read-only over event_log + config_regime. Run from backend/
(env: DATABASE_URL / BFX_EXCHANGE_ACCOUNT_ID / BFX_DEPLOYMENT_ENV):
  uv run python -m scripts.report_execution_quality
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings, require_deployment_environment
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_canonical,
    account_scope_clause,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.live_validation.execution_quality import (
    ClaimEvent,
    FillEvent,
    bucket_by_regime,
    pair_claims_to_fills,
    summarize,
)
from bfx_funding_bot.modules.live_validation.tables import ConfigRegimeRow

_TTL_MS = int(os.environ.get("BFX_QUOTE_TTL_MS", "3900000"))


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _fmt_min(ms: int | None) -> str:
    return f"{ms / 60_000:.1f}m" if ms is not None else "-"


async def _amain() -> int:
    settings = Settings()
    engine = make_engine(settings)
    sf = make_session_factory(engine)
    raw_account_id = os.environ.get("BFX_EXCHANGE_ACCOUNT_ID", "").strip()
    if not raw_account_id:
        raise RuntimeError("BFX_EXCHANGE_ACCOUNT_ID is required")
    account_id = account_id_canonical(raw_account_id)
    env = require_deployment_environment()
    try:
        async with sf() as session:
            def _events(event_type: str) -> Any:
                return (
                    select(EventLogRow)
                    .where(
                        EventLogRow.event_type == event_type,
                        account_scope_clause(
                            session,
                            account_id=account_id,
                            exchange_account_column=EventLogRow.exchange_account_id,
                            legacy_account_column=EventLogRow.account_id,
                        ),
                        EventLogRow.deployment_environment == env,
                        EventLogRow.cid.is_not(None),
                    )
                    .order_by(EventLogRow.occurred_at_ms)
                )
            claim_rows = (await session.execute(_events("RESERVATION_CLAIMED"))).scalars().all()
            fill_rows = (await session.execute(_events("ORDER_FILL"))).scalars().all()
            regime_rows = (
                await session.execute(
                    select(ConfigRegimeRow).where(
                        account_scope_clause(
                            session,
                            account_id=account_id,
                            exchange_account_column=ConfigRegimeRow.exchange_account_id,
                            legacy_account_column=ConfigRegimeRow.account_id,
                        ),
                        ConfigRegimeRow.deployment_environment == env,
                    ).order_by(ConfigRegimeRow.recorded_at_ms)
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    outcomes = pair_claims_to_fills(
        [ClaimEvent(cid=r.cid, claimed_at_ms=r.occurred_at_ms) for r in claim_rows],
        [FillEvent(cid=r.cid, filled_at_ms=r.occurred_at_ms) for r in fill_rows],
    )
    if not regime_rows:
        print("no config_regime rows yet — deploy Task 4 first; showing single bucket")
        regime_starts = [0]
        flags_by_start = {0: ("?", "?")}
    else:
        regime_starts = [r.recorded_at_ms for r in regime_rows]
        flags_by_start = {
            r.recorded_at_ms: (str(r.clamp_enabled), str(r.reprice_enabled))
            for r in regime_rows
        }
    summaries = summarize(
        bucket_by_regime(outcomes, regime_starts=regime_starts), ttl_ms=_TTL_MS,
    )
    print(f"execution quality — account={account_id} env={env} ttl={_TTL_MS}ms")
    print("regime_start (UTC)  clamp reprice  claims filled fill%   p50    p90  unfilled>TTL")
    for s in summaries:
        clamp, reprice = flags_by_start.get(s.regime_start_ms, ("?", "?"))
        print(
            f"{_fmt_ts(s.regime_start_ms):19} {clamp:5} {reprice:7} "
            f"{s.n_claims:6} {s.n_filled:6} {float(s.fill_rate) * 100:5.1f} "
            f"{_fmt_min(s.p50_latency_ms):>6} {_fmt_min(s.p90_latency_ms):>6} "
            f"{s.n_unfilled_past_ttl:12}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
