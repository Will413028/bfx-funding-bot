"""config_regime writer — one best-effort row per daemon boot."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.accounts.exchange_accounts import account_id_uuid_or_none
from bfx_funding_bot.modules.live_validation.tables import ConfigRegimeRow

log = logging.getLogger(__name__)


async def record_config_regime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: str,
    deployment_environment: str,
    clamp_enabled: bool,
    reprice_enabled: bool,
    git_sha: str | None,
    now_ms: int,
) -> None:
    """Insert this boot's execution-policy regime row. Best-effort: telemetry
    must never block or kill the boot — any failure logs and returns."""
    try:
        async with session_factory() as session:
            session.add(ConfigRegimeRow(
                deployment_environment=deployment_environment,
                account_id=account_id,
                exchange_account_id=account_id_uuid_or_none(account_id),
                recorded_at_ms=now_ms,
                clamp_enabled=clamp_enabled,
                reprice_enabled=reprice_enabled,
                git_sha=git_sha,
            ))
            await session.commit()
        log.info(
            "config_regime_recorded clamp=%s reprice=%s sha=%s",
            clamp_enabled, reprice_enabled, git_sha,
        )
    except Exception:
        log.warning("config_regime_record_failed", exc_info=True)
