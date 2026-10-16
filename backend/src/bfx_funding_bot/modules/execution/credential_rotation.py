"""Operator rotation of the account's Bitfinex key, applied by the account daemon.

ADR 2026-08-31 Amendment D5' and ADR 2026-10-15 D4: the operator CLI never holds the
vault KEK. It seals the new key to the public key this bot run published
(``credential_rotation_recipients``, ``core.sealed_credential``) and queues a request in
``credential_rotation_requests`` naming the active credential it replaces; this worker
opens it, verifies it and swaps the active credential.

Settling one request (``OperatorRequestWorker``):

1. under the account lock the operator is re-checked (an unauthorized request is
   rejected before anything is decrypted or sent), and :meth:`apply` asks for preparation;
2. :meth:`prepare` runs with no lock or transaction and only observes: no deployment is
   settling, the request was sealed to this run, it opens, the key was never retired or
   revoked, its permissions are funding-write only, and it belongs to the same Bitfinex
   user as the key in use. Its verdict is returned, never raised: a refusal raised there
   would be recorded as ``failed``;
3. under the lock again (operator re-checked), :meth:`apply` raises the refusal as
   ``rejected``, or retires the replaced credential and inserts the verified one as active
   in the same transaction that records ``applied`` (``vault.rotate_active_credential``).

After an applied rotation commits, the process stops the way SIGTERM stops it (``restart``
sets the daemon's stop event) and the container restart policy boots it on the new key,
read by ``load_account_credentials`` as on every boot. Until then the old key, still valid
at Bitfinex, keeps trading: there is no moment without a key.

Nothing here logs, alerts or raises with the key's secret; it is held in :class:`_Secret`.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.sealed_credential import (
    Recipient,
    SealContext,
    SealedPayloadError,
    UnsealedCredential,
    generate_recipient,
    open_sealed,
)
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    BitfinexShapeError,
    venue_did_not_answer,
)
from bfx_funding_bot.modules.accounts.rotation_tables import (
    CredentialRotationRecipientRow,
    CredentialRotationRequestRow,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccountCredential
from bfx_funding_bot.modules.accounts.vault import (
    ActiveCredentialChangedError,
    permission_error,
    rotate_active_credential,
)
from bfx_funding_bot.modules.deployments.tables import DEPLOYMENT_STARTED, DeploymentRow
from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    REJECTED,
    NeedsPreparation,
    OperatorAuthority,
    OperatorRequestWorker,
    Outcome,
    RequestRejected,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

# How long the newest ``started`` deployments row counts as a deploy in progress. A restart
# while bfx-deploy waits for health and then settles fails that deploy
# (``restarted_during_settle``); its defaults are a 300s bot health timeout and a 60s settle
# window (deploy/vm/ops/bfx_deploy.py:570-574), plus margin. A deploy that was interrupted
# (a ``started`` row with no terminal row, never retried) blocks rotation for at most this
# long.
DEPLOYMENT_IN_PROGRESS_WINDOW = timedelta(minutes=15)
# How long after this run started a rotation is refused. bfx-deploy writes its ``started``
# row only after backup, restore test and migrations, yet stamps it with the attempt's
# start, so the window above can lapse while the new bot is still being health-checked
# and settled (bot health 300s + settle 60s by default, deploy/vm/ops/bfx_deploy.py:570-574,
# plus margin). The bot being settled is always the one that just started; after any
# restart a rotation waits this long.
BOT_SETTLE_WINDOW = timedelta(minutes=10)

DEPLOYMENT_IN_PROGRESS = "deployment_in_progress"
BOT_RECENTLY_STARTED = "bot_recently_started"
RECIPIENT_KEY_STALE = "recipient_key_stale"
SEALED_PAYLOAD_INVALID = "sealed_payload_invalid"
CREDENTIAL_REUSED = "credential_reused"
INVALID_CREDENTIALS = "invalid_credentials"
ACCOUNT_IDENTITY_UNAVAILABLE = "account_identity_unavailable"
CREDENTIAL_ACCOUNT_MISMATCH = "credential_account_mismatch"
ACTIVE_CREDENTIAL_CHANGED = "active_credential_changed"
REQUEST_NOT_PENDING = "request_not_pending"


class KeyClient(Protocol):
    """What verifying a key reads from Bitfinex (``BitfinexAuthREST``)."""

    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions: ...

    async def get_user_id(self, *, ctx: AccountContext) -> int: ...


class _Secret:
    """A secret that never renders: repr, str and format all show a mask."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "_Secret(***)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class Verified:
    """The new key passed every check; ``user_id`` is its Bitfinex account."""

    api_key: str = field(repr=False)
    secret: _Secret
    user_id: int
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class Refused:
    """A bounded reason the rotation is rejected, decided before the lock."""

    code: str


@dataclass(frozen=True, slots=True)
class PreparedRotation:
    request_id: UUID
    verdict: Verified | Refused


class CredentialStoreError(RuntimeError):
    """A database error while reading or writing credentials, named by its DBAPI type only.

    SQLAlchemy's own message carries the statement's parameters -- the new API key among
    them -- and the driver logs failures with their traceback; this one carries neither.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _store_error(exc: StatementError) -> CredentialStoreError:
    return CredentialStoreError(type(exc.orig).__name__ if exc.orig is not None
                                else type(exc).__name__)


class CredentialRotationWorker(OperatorRequestWorker[CredentialRotationRequestRow, PreparedRotation]):
    """Applies credential rotations, oldest first; an applied one restarts the bot."""

    model = CredentialRotationRequestRow
    name = "credential_rotation"

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        account_id: UUID,
        environment: str,
        authority: OperatorAuthority,
        client_factory: Callable[[], KeyClient],
        current_client: KeyClient,
        current_ctx: AccountContext,
        kek_loader: Callable[[], bytes],
        restart: Callable[[], None],
        clock: Callable[[], int] | None = None,
        ownership: Callable[[], Awaitable[bool]] | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        super().__init__(session_factory=session_factory, account_id=account_id,
                         environment=environment, authority=authority, clock=clock,
                         ownership=ownership, poll_interval_s=poll_interval_s)
        # A client on its own nonce gate per verification: the new key's nonces must not
        # interleave with the key in use, which keeps the daemon's shared gate.
        self.client_factory = client_factory
        self.current_client = current_client
        self.current_ctx = current_ctx
        self.kek_loader = kek_loader
        self.restart = restart
        self._run_id: UUID | None = None
        self._run_started_ms: int | None = None
        self._recipient: tuple[UUID, Recipient] | None = None
        self._restart_requested = False

    # ------------------------------------------------------------- recipient

    def bind_run(self, run_id: UUID, started_at_ms: int) -> None:
        """This process's ``bot_runs`` row; the recipient key is published under it, and no
        rotation applies until the run is :data:`BOT_SETTLE_WINDOW` old."""
        self._run_id = run_id
        self._run_started_ms = started_at_ms

    def failure_reason(self, exc: BaseException) -> str:
        if isinstance(exc, CredentialStoreError):
            return f"{self.name}_failed:{exc.kind}"
        return super().failure_reason(exc)

    def _request_restart(self, event: str, request_id: UUID) -> None:
        if self._restart_requested:
            return
        self._restart_requested = True
        log.info("%s request_id=%s", event, request_id)
        self.restart()

    @property
    def recipient_id(self) -> UUID | None:
        return None if self._recipient is None else self._recipient[0]

    async def idle(self) -> None:
        """Publish this run's recipient key once (a failure is retried on the next tick), then
        catch up on a rotation that committed without its restart."""
        if self._run_id is None:
            return
        if self._recipient is None:
            await self._publish_recipient()
        await self._catch_up_restart()

    async def _catch_up_restart(self) -> None:
        """Restart when the active credential came from a rotation applied during this run.

        ``committed`` restarts after an applied rotation; when the commit landed but its
        acknowledgement was lost, the driver marks nothing and calls no hook, so the new
        credential would wait for an unrelated restart. The rotation that caused this boot
        was applied before the run started and is left alone.
        """
        if self._restart_requested or self._run_started_ms is None:
            return
        try:
            async with self.session_factory() as session:
                applied = (await session.execute(select(
                    CredentialRotationRequestRow.request_id,
                    CredentialRotationRequestRow.processed_at_ms,
                ).join(ExchangeAccountCredential,
                       ExchangeAccountCredential.operator_request_id
                       == CredentialRotationRequestRow.request_id).where(
                    ExchangeAccountCredential.exchange_account_id == self.account_id,
                    ExchangeAccountCredential.venue == "bitfinex",
                    ExchangeAccountCredential.lifecycle_status == "active",
                    CredentialRotationRequestRow.state == APPLIED,
                ).limit(1))).first()
        except Exception as exc:
            log.warning("credential_rotation_catch_up_failed reason=%s", type(exc).__name__)
            return
        if (applied is not None and applied.processed_at_ms is not None
                and applied.processed_at_ms >= self._run_started_ms):
            self._request_restart("credential_rotation_restart_caught_up", applied.request_id)

    async def _publish_recipient(self) -> None:
        recipient_id, recipient = uuid4(), generate_recipient()
        try:
            async with self.session_factory.begin() as session:
                session.add(CredentialRotationRecipientRow(
                    recipient_id=recipient_id, exchange_account_id=self.account_id,
                    deployment_environment=self.environment, run_id=self._run_id,
                    public_key=recipient.public_key, created_at_ms=self.clock()))
        except Exception as exc:
            log.warning("credential_rotation_recipient_unpublished reason=%s", type(exc).__name__)
            return
        self._recipient = (recipient_id, recipient)
        log.info("credential_rotation_recipient_published recipient_id=%s run_id=%s",
                 recipient_id, self._run_id)

    # --------------------------------------------------------------- prepare

    async def prepare(self, row_id: UUID) -> PreparedRotation:
        # What the database says first, in a session closed before Bitfinex is called.
        try:
            async with self.session_factory() as session:
                row = await session.scalar(self._scoped(select(self.model)).where(
                    self.model.request_id == row_id))
                local = await self._judge_locally(session, row)
        except StatementError as exc:
            raise _store_error(exc) from None
        if isinstance(local, Refused):
            return PreparedRotation(row_id, local)
        return PreparedRotation(row_id, await self._verify(local))

    async def _judge_locally(self, session: AsyncSession,
                             row: CredentialRotationRequestRow | None,
                             ) -> UnsealedCredential | Refused:
        if row is None or row.state != "requested":
            return Refused(REQUEST_NOT_PENDING)
        if await self._deployment_in_progress(session):
            return Refused(DEPLOYMENT_IN_PROGRESS)
        if self._run_started_ms is None or (
                self.clock() - self._run_started_ms
                < BOT_SETTLE_WINDOW.total_seconds() * 1000):
            return Refused(BOT_RECENTLY_STARTED)
        if self._recipient is None or row.recipient_id != self._recipient[0]:
            return Refused(RECIPIENT_KEY_STALE)
        try:
            opened = open_sealed(row.sealed_credential, private_key=self._recipient[1].private_half,
                                 context=SealContext(self.account_id, self.environment,
                                                     row.recipient_id, row.request_id))
        except SealedPayloadError:
            return Refused(SEALED_PAYLOAD_INVALID)
        # Retired and revoked credentials are terminal; the active one is no rotation.
        reused = await session.scalar(select(func.count()).select_from(
            ExchangeAccountCredential).where(
            ExchangeAccountCredential.venue == "bitfinex",
            ExchangeAccountCredential.api_key == opened.api_key,
            ExchangeAccountCredential.lifecycle_status != "pending"))
        if reused:
            return Refused(CREDENTIAL_REUSED)
        return opened

    async def _verify(self, opened: UnsealedCredential) -> Verified | Refused:
        """Bitfinex's view of the new key. When the venue did not answer (transport, rate
        limit, maintenance, a 5xx without its error body) this raises (``failed``, resend);
        its answer, even with HTTP 500 (``["error", 10100, ...]``), is a refusal."""
        ctx = AccountContext(account_id=str(self.account_id),
                             credentials=Credentials(api_key=opened.api_key,
                                                     api_secret=opened.api_secret))
        client = self.client_factory()
        try:
            perms = await client.get_key_permissions(ctx=ctx)
        except BitfinexAPIError as exc:
            if venue_did_not_answer(exc):
                raise
            return Refused(INVALID_CREDENTIALS)
        refusal = permission_error(perms)
        if refusal is not None:
            return Refused(refusal)
        try:
            new_user = await client.get_user_id(ctx=ctx)
            current_user = await self.current_client.get_user_id(ctx=self.current_ctx)
        except BitfinexAPIError as exc:
            if venue_did_not_answer(exc):
                raise
            return Refused(ACCOUNT_IDENTITY_UNAVAILABLE)
        except BitfinexShapeError:
            return Refused(ACCOUNT_IDENTITY_UNAVAILABLE)
        if new_user != current_user:
            return Refused(CREDENTIAL_ACCOUNT_MISMATCH)
        return Verified(api_key=opened.api_key, secret=_Secret(opened.api_secret),
                        user_id=new_user, verified_at=self._now())

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock() / 1000, UTC)

    async def _deployment_in_progress(self, session: AsyncSession) -> bool:
        """The newest deployments row is a recent ``started`` one (the table is append-only:
        every attempt keeps its ``started`` row, a terminal row follows it)."""
        newest = (await session.execute(select(DeploymentRow.outcome, DeploymentRow.started_at)
                                        .order_by(DeploymentRow.id.desc()).limit(1))).first()
        if newest is None or newest.outcome != DEPLOYMENT_STARTED:
            return False
        started_at: datetime = newest.started_at
        if started_at.tzinfo is None:  # SQLite fixtures drop the zone
            started_at = started_at.replace(tzinfo=UTC)
        elapsed: timedelta = self._now() - started_at
        return elapsed < DEPLOYMENT_IN_PROGRESS_WINDOW

    # ----------------------------------------------------------------- apply

    async def apply(self, session: AsyncSession, row: CredentialRotationRequestRow,
                    prepared: PreparedRotation | None) -> Outcome:
        if prepared is None:
            raise NeedsPreparation
        if prepared.request_id != row.request_id:
            raise RuntimeError("prepared_for_another_request")
        verdict = prepared.verdict
        if isinstance(verdict, Refused):
            raise RequestRejected(verdict.code)
        try:
            credential = await self._rotate(session, row, verdict)
        except ActiveCredentialChangedError as exc:
            raise RequestRejected(ACTIVE_CREDENTIAL_CHANGED) from exc
        return Outcome(APPLIED, f"rotated: credential {credential.id}", detail=credential.id)

    async def _rotate(self, session: AsyncSession, row: CredentialRotationRequestRow,
                      verdict: Verified) -> ExchangeAccountCredential:
        try:
            return await rotate_active_credential(
                session, exchange_account_id=self.account_id,
                replaces_credential_id=row.replaces_credential_id,
                operator_request_id=row.request_id, label=row.label, api_key=verdict.api_key,
                api_secret=verdict.secret.reveal(), verified_at=verdict.verified_at,
                kek=self.kek_loader())
        except StatementError as exc:
            raise _store_error(exc) from None

    async def committed(self, row: CredentialRotationRequestRow, outcome: Outcome) -> None:
        fields: dict[str, Any] = {"request_id": str(row.request_id),
                                  "replaces_credential_id": str(row.replaces_credential_id)}
        if outcome.state == APPLIED:
            try:
                alerts.emit(alerts.CREDENTIAL_ROTATION_APPLIED, level=alerts.WARNING,
                            credential_id=str(outcome.detail), reason=outcome.reason, **fields)
            finally:
                # Also when the alert fails: the new credential is the active one now, and
                # only a boot loads it.
                self._request_restart("credential_rotation_restart", row.request_id)
        elif outcome.state == REJECTED:
            alerts.emit(alerts.CREDENTIAL_ROTATION_REJECTED, level=alerts.WARNING,
                        reason=outcome.reason, **fields)
        else:
            alerts.emit(alerts.CREDENTIAL_ROTATION_FAILED, level=alerts.CRITICAL,
                        reason=outcome.reason, **fields)


__all__ = [
    "BOT_SETTLE_WINDOW",
    "DEPLOYMENT_IN_PROGRESS_WINDOW",
    "CredentialRotationWorker",
    "CredentialStoreError",
    "KeyClient",
    "PreparedRotation",
    "Refused",
    "Verified",
]
