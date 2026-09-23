"""config_regime writer — one best-effort row per daemon boot."""
from __future__ import annotations

import logging

from sqlalchemy import select
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
            exchange_account_id = account_id_uuid_or_none(account_id)
            if exchange_account_id is None:
                # Historical SQLite fixtures use synthetic realms. Preserve
                # the one-row-per-boot best-effort contract there without
                # relying on NULL composite-PK uniqueness; production daemon
                # boots always supply the canonical UUID.
                existing = await session.scalar(
                    select(ConfigRegimeRow).where(
                        ConfigRegimeRow.account_id == account_id,
                        ConfigRegimeRow.deployment_environment == deployment_environment,
                        ConfigRegimeRow.recorded_at_ms == now_ms,
                    )
                )
                if existing is not None:
                    return
            session.add(ConfigRegimeRow(
                deployment_environment=deployment_environment,
                account_id=account_id,
                exchange_account_id=exchange_account_id,
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
