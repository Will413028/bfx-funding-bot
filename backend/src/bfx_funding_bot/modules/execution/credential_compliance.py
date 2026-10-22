"""Whether the key the bot runs on is still the key it may trade with (9g).

A credential rotation verifies a new key before it becomes active
(``credential_rotation.CredentialRotationWorker._verify``); this rechecks the key in use by the
same rule, at boot and every :data:`CREDENTIAL_RECHECK_INTERVAL_S` while the bot runs:

1. its Bitfinex permissions allow funding writes and no other write
   (``accounts.vault.permission_error``: withdraw, orders, an unknown write scope refuse it);
2. it belongs to the Bitfinex user stored on its own active credential row
   (``bitfinex_user_id``); a row without one cannot be compared and refuses it too.

A key that fails is :class:`CredentialNoncompliantError`. It is handled exactly like the venue
refusing the key (ADR 2026-10-18 D11: no separate stop state): at boot the daemon holds in
credential recovery mode, and mid-run the daemon exits 78 so the next boot does. What the
venue itself answers is not decided here: the client's errors propagate unchanged (10100 is
the venue refusing the key; an unanswered request is transient), as in the boot observation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.modules.accounts.tables import ExchangeAccountCredential
from bfx_funding_bot.modules.accounts.vault import permission_error
from bfx_funding_bot.modules.execution.credential_rotation import (
    ACCOUNT_IDENTITY_UNAVAILABLE,
    CREDENTIAL_ACCOUNT_MISMATCH,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext

log = logging.getLogger(__name__)

# How often a running bot rechecks its key (plan step 9g). A permission change needs the
# account owner at Bitfinex, so this bounds how long a widened key keeps trading unnoticed.
CREDENTIAL_RECHECK_INTERVAL_S = 6 * 3600.0
# After a recheck the venue did not answer: try again sooner than the interval.
CREDENTIAL_RECHECK_RETRY_S = 300.0


class CredentialNoncompliantError(Exception):
    """The key in use may not trade: ``reason`` is the rotation's refusal reason for it
    (``withdraw_must_be_disabled``, ``funding_write_required``, ``unexpected_write_scope:...``,
    ``credential_account_mismatch``, ``account_identity_unavailable``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"credential_noncompliant:{reason}")
        self.reason = reason


class ComplianceClient(Protocol):
    """What checking the key reads from Bitfinex (``BitfinexAuthREST``)."""

    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions: ...

    async def get_user_id(self, *, ctx: AccountContext) -> int: ...


@dataclass
class CredentialCompliance:
    """Checks the key in ``ctx`` (the one the bot booted on) against its active credential row."""

    session_factory: async_sessionmaker[AsyncSession]
    exchange_account_id: UUID
    client: ComplianceClient
    ctx: AccountContext = field(repr=False)

    async def check(self) -> None:
        """Return when the key complies; raise :class:`CredentialNoncompliantError` when it does
        not. Venue errors (refused, unanswered) propagate as the client raised them."""
        async with self.session_factory() as session:
            stored_user_id = await session.scalar(
                select(ExchangeAccountCredential.bitfinex_user_id).where(
                    ExchangeAccountCredential.exchange_account_id == self.exchange_account_id,
                    ExchangeAccountCredential.venue == "bitfinex",
                    ExchangeAccountCredential.lifecycle_status == "active",
                    ExchangeAccountCredential.api_key == self.ctx.credentials.api_key,
                ))
        # The rotation's order: the stored identity first (nothing is asked without it), then
        # the permissions, then the key's own user id.
        if stored_user_id is None:
            raise CredentialNoncompliantError(ACCOUNT_IDENTITY_UNAVAILABLE)
        refusal = permission_error(await self.client.get_key_permissions(ctx=self.ctx))
        if refusal is not None:
            raise CredentialNoncompliantError(refusal)
        try:
            user_id = await self.client.get_user_id(ctx=self.ctx)
        except BitfinexShapeError:
            raise CredentialNoncompliantError(ACCOUNT_IDENTITY_UNAVAILABLE) from None
        if user_id != stored_user_id:
            raise CredentialNoncompliantError(CREDENTIAL_ACCOUNT_MISMATCH)
        log.info("credential_compliance_ok account=%s", self.exchange_account_id)
