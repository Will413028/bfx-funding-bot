"""Idempotent Halt 1 identity mapping and credential/config cutover.

Alembic owns only the additive schema.  This module owns the application data
movement so the operator can validate a manifest, run a dry-run, and repeat an
interrupted cutover without losing or logging secret material.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import (
    Envelope,
    decrypt_secret,
    decrypt_secret_with_aad,
    encrypt_secret_with_aad,
)
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountRole,
    account_id_canonical,
    validate_membership_role,
)
from bfx_funding_bot.modules.accounts.tables import (
    AccountConfigDraft,
    APIKey,
    BillingRecord,
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
    Execution,
    LegacyAccountRealmMap,
    User,
    UserConfig,
)
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    ReconcileObservationRow,
)
from bfx_funding_bot.modules.execution.safety.tables import NavPeakRow, TradingHaltRow
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow, ConfigRegimeRow

_MONEY_MODELS: tuple[tuple[str, type[Any]], ...] = (
    ("event_log", EventLogRow),
    ("offer_claims", OfferClaimRow),
    ("position_state", PositionStateRow),
    ("reconcile_observation", ReconcileObservationRow),
    ("execution_decisions", ExecutionDecisionRow),
    ("diagnostics", DiagnosticsRow),
    ("nav_peak", NavPeakRow),
    ("trading_halt", TradingHaltRow),
    ("attribution_weekly", AttributionWeeklyRow),
    ("config_regime", ConfigRegimeRow),
)
_LEGACY_ZERO_ROW_MODELS: tuple[tuple[str, type[Any]], ...] = (
    ("users", User),
    ("executions", Execution),
    ("billing_records", BillingRecord),
)


class IdentityCutoverError(RuntimeError):
    """Base class for manifest, preflight, and verification failures."""


class CutoverManifestError(IdentityCutoverError):
    """The mapping manifest is malformed or cannot cover legacy data."""


class CutoverVerificationError(IdentityCutoverError):
    """The post-cutover invariants are not yet safe for contract migration."""


@dataclass(frozen=True, slots=True)
class AccountMapping:
    exchange_account_id: UUID
    venue: str
    label: str
    legacy_realms: tuple[str, ...]
    user_ids: tuple[str, ...]
    memberships: tuple[tuple[str, AccountRole], ...]

    @property
    def membership_map(self) -> dict[str, AccountRole]:
        return dict(self.memberships)


@dataclass(frozen=True, slots=True)
class IdentityManifest:
    version: int
    accounts: tuple[AccountMapping, ...]
    sha256: str

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> IdentityManifest:
        version = value.get("version")
        if version != 1:
            raise CutoverManifestError("identity manifest version must be 1")
        raw_accounts = value.get("accounts")
        if not isinstance(raw_accounts, Sequence) or isinstance(raw_accounts, (str, bytes)):
            raise CutoverManifestError("identity manifest accounts must be an array")

        accounts: list[AccountMapping] = []
        seen_account_ids: set[UUID] = set()
        seen_realms: set[str] = set()
        seen_users: set[str] = set()
        for raw in raw_accounts:
            if not isinstance(raw, Mapping):
                raise CutoverManifestError("each account mapping must be an object")
            account_id_raw = raw.get("exchange_account_id")
            if not isinstance(account_id_raw, str):
                raise CutoverManifestError("exchange_account_id must be a UUID string")
            try:
                account_id = UUID(account_id_raw)
            except ValueError as exc:
                raise CutoverManifestError("exchange_account_id must be a UUID string") from exc
            if account_id in seen_account_ids:
                raise CutoverManifestError(f"duplicate exchange_account_id {account_id}")
            seen_account_ids.add(account_id)

            venue = raw.get("venue", "bitfinex")
            label = raw.get("label")
            if not isinstance(venue, str) or not venue:
                raise CutoverManifestError("account venue must be a non-empty string")
            if not isinstance(label, str) or not label:
                raise CutoverManifestError("account label must be a non-empty string")

            legacy_realms = _string_tuple(raw.get("legacy_realms"), "legacy_realms")
            if not legacy_realms:
                raise CutoverManifestError(f"account {account_id} has no legacy realms")
            for realm in legacy_realms:
                if realm in seen_realms:
                    raise CutoverManifestError(f"legacy realm appears more than once: {realm}")
                seen_realms.add(realm)

            user_ids = _string_tuple(raw.get("user_ids", ()), "user_ids")
            for user_id in user_ids:
                if user_id in seen_users:
                    raise CutoverManifestError(f"user appears more than once: {user_id}")
                seen_users.add(user_id)

            raw_memberships = raw.get("memberships", {})
            if not isinstance(raw_memberships, Mapping):
                raise CutoverManifestError("memberships must be an object keyed by user id")
            memberships: list[tuple[str, AccountRole]] = []
            for user_id, raw_role in raw_memberships.items():
                if not isinstance(user_id, str) or not isinstance(raw_role, str):
                    raise CutoverManifestError("membership user and role must be strings")
                if user_id not in user_ids:
                    raise CutoverManifestError(
                        f"membership user {user_id!r} is absent from user_ids"
                    )
                memberships.append((user_id, validate_membership_role(raw_role)))
            if user_ids and not memberships:
                memberships.append((user_ids[0], "owner"))
            memberships.sort(key=lambda item: item[0])
            accounts.append(
                AccountMapping(
                    exchange_account_id=account_id,
                    venue=venue,
                    label=label,
                    legacy_realms=tuple(sorted(legacy_realms)),
                    user_ids=tuple(sorted(user_ids)),
                    memberships=tuple(memberships),
                )
            )

        if not accounts:
            raise CutoverManifestError("identity manifest must contain at least one account")
        normalized = {
            "version": 1,
            "accounts": [
                {
                    "exchange_account_id": str(account.exchange_account_id),
                    "venue": account.venue,
                    "label": account.label,
                    "legacy_realms": list(account.legacy_realms),
                    "user_ids": list(account.user_ids),
                    "memberships": dict(account.memberships),
                }
                for account in sorted(accounts, key=lambda item: str(item.exchange_account_id))
            ],
        }
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return cls(version=1, accounts=tuple(accounts), sha256=hashlib.sha256(encoded).hexdigest())

    @property
    def realm_to_account(self) -> dict[str, UUID]:
        return {
            realm: account.exchange_account_id
            for account in self.accounts
            for realm in account.legacy_realms
        }

    @property
    def user_to_account(self) -> dict[str, UUID]:
        return {
            user_id: account.exchange_account_id
            for account in self.accounts
            for user_id in account.user_ids
        }


@dataclass(frozen=True, slots=True)
class CutoverReport:
    manifest_sha256: str
    dry_run: bool = False
    legacy_realm_counts: dict[str, int] = field(default_factory=dict)
    unmapped_rows: tuple[str, ...] = ()
    duplicate_active_credentials: tuple[str, ...] = ()
    zero_row_legacy_tables: dict[str, bool] = field(default_factory=dict)
    event_head: int | None = None
    event_hash: str = ""
    backfilled_rows: dict[str, int] = field(default_factory=dict)
    migrated_credentials: int = 0
    migrated_configs: int = 0


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CutoverManifestError(f"{field_name} must be an array of strings")
    if not all(isinstance(item, str) and item for item in value):
        raise CutoverManifestError(f"{field_name} must contain non-empty strings")
    return tuple(cast(str, item) for item in value)


def _coerce_manifest(value: IdentityManifest | Mapping[str, object]) -> IdentityManifest:
    if isinstance(value, IdentityManifest):
        return value
    return IdentityManifest.from_dict(value)


class IdentityCutover:
    """Run the Halt 1 application data migration for one validated manifest."""

    def __init__(self, *, kek: bytes, failure_after_table: str | None = None) -> None:
        self._kek = kek
        self._failure_after_table = failure_after_table

    async def preflight(
        self,
        session: AsyncSession,
        manifest: IdentityManifest | Mapping[str, object],
    ) -> CutoverReport:
        return await self._preflight(session, _coerce_manifest(manifest))

    async def apply(
        self,
        session: AsyncSession,
        manifest: IdentityManifest | Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> CutoverReport:
        normalized = _coerce_manifest(manifest)
        report = await self._preflight(session, normalized)
        self._assert_preflight_ready(report)

        transaction = await session.begin_nested()
        try:
            await self._upsert_identity_rows(session, normalized)
            credentials = await self._migrate_credentials(session, normalized)
            configs = await self._migrate_configs(session, normalized)
            backfilled = await self._backfill_money_rows(session, normalized)
            result = replace(
                report,
                dry_run=dry_run,
                backfilled_rows=backfilled,
                migrated_credentials=credentials,
                migrated_configs=configs,
            )
            if dry_run:
                await transaction.rollback()
            else:
                await transaction.commit()
            return result
        except Exception:
            await transaction.rollback()
            raise

    async def verify(
        self,
        session: AsyncSession,
        manifest: IdentityManifest | Mapping[str, object],
    ) -> CutoverReport:
        normalized = _coerce_manifest(manifest)
        report = await self._preflight(session, normalized)
        self._assert_preflight_ready(report)
        null_rows: list[str] = []
        for table_name, model in _MONEY_MODELS:
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.exchange_account_id.is_(None))
            )
            if count:
                null_rows.append(table_name)
        for table_name, model in (("api_keys", APIKey), ("user_configs", UserConfig)):
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.exchange_account_id.is_(None))
            )
            if count:
                null_rows.append(table_name)
        if null_rows:
            raise CutoverVerificationError(
                f"exchange_account_id remains NULL in {', '.join(sorted(null_rows))}"
            )

        credentials = list(await session.scalars(select(ExchangeAccountCredential)))
        for credential in credentials:
            envelope = Envelope(
                secret_ciphertext=credential.secret_ciphertext,
                secret_nonce=credential.secret_nonce,
                wrapped_dek=credential.wrapped_dek,
                dek_nonce=credential.dek_nonce,
                key_version=credential.key_version,
            )
            decrypt_secret_with_aad(
                envelope,
                aad=account_id_canonical(credential.exchange_account_id),
                kek=self._kek,
            )
        return report

    async def _preflight(
        self, session: AsyncSession, manifest: IdentityManifest
    ) -> CutoverReport:
        realm_to_account = manifest.realm_to_account
        legacy_realm_counts: defaultdict[str, int] = defaultdict(int)
        unmapped: set[str] = set()
        for table_name, model in _MONEY_MODELS:
            values = await session.scalars(select(model.account_id).distinct())
            for realm in values:
                if realm not in realm_to_account:
                    unmapped.add(f"{table_name}:{realm}")
                else:
                    count = await session.scalar(
                        select(func.count()).select_from(model).where(model.account_id == realm)
                    )
                    legacy_realm_counts[realm] += int(count or 0)

        user_models = (("api_keys", APIKey), ("user_configs", UserConfig))
        for table_name, model in user_models:
            values = await session.scalars(select(model.user_id).distinct())
            for user_id in values:
                if user_id not in manifest.user_to_account:
                    unmapped.add(f"{table_name}:user:{user_id}")

        duplicate_credentials = await self._duplicate_credential_accounts(session, manifest)
        zero_row_legacy_tables = {
            table_name: bool(
                (await session.scalar(select(func.count()).select_from(model))) == 0
            )
            for table_name, model in _LEGACY_ZERO_ROW_MODELS
        }
        events = list(
            await session.scalars(select(EventLogRow).order_by(EventLogRow.event_seq.asc()))
        )
        event_payload = [
            {
                "event_seq": row.event_seq,
                "account_id": row.account_id,
                "deployment_environment": row.deployment_environment,
                "event_type": row.event_type,
                "cid": row.cid,
                "venue_offer_id": row.venue_offer_id,
                "venue_seq": row.venue_seq,
                "payload": row.payload,
                "occurred_at_ms": row.occurred_at_ms,
            }
            for row in events
        ]
        encoded_events = json.dumps(
            event_payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return CutoverReport(
            manifest_sha256=manifest.sha256,
            legacy_realm_counts=dict(legacy_realm_counts),
            unmapped_rows=tuple(sorted(unmapped)),
            duplicate_active_credentials=tuple(sorted(duplicate_credentials)),
            zero_row_legacy_tables=zero_row_legacy_tables,
            event_head=events[-1].event_seq if events else None,
            event_hash=hashlib.sha256(encoded_events).hexdigest(),
        )

    @staticmethod
    async def _duplicate_credential_accounts(
        session: AsyncSession, manifest: IdentityManifest
    ) -> set[str]:
        counts: defaultdict[UUID, int] = defaultdict(int)
        rows = await session.scalars(select(APIKey).where(APIKey.exchange_account_id.is_(None)))
        for row in rows:
            account_id = manifest.user_to_account.get(row.user_id)
            if account_id is not None:
                counts[account_id] += 1
        target_rows = await session.scalars(
            select(ExchangeAccountCredential).where(
                ExchangeAccountCredential.lifecycle_status == "active"
            )
        )
        for credential_row in target_rows:
            counts[credential_row.exchange_account_id] += 1
        return {str(account_id) for account_id, count in counts.items() if count > 1}

    @staticmethod
    def _assert_preflight_ready(report: CutoverReport) -> None:
        if report.unmapped_rows:
            raise CutoverManifestError(
                "identity manifest leaves unmapped rows: " + ", ".join(report.unmapped_rows)
            )
        if report.duplicate_active_credentials:
            raise CutoverManifestError(
                "duplicate active credentials for accounts: "
                + ", ".join(report.duplicate_active_credentials)
            )

    async def _upsert_identity_rows(
        self, session: AsyncSession, manifest: IdentityManifest
    ) -> None:
        for account_mapping in manifest.accounts:
            account = await session.get(ExchangeAccount, account_mapping.exchange_account_id)
            if account is None:
                account = ExchangeAccount(
                    id=account_mapping.exchange_account_id,
                    venue=account_mapping.venue,
                    label=account_mapping.label,
                    lifecycle_status="active",
                )
                session.add(account)
            elif account.lifecycle_status == "retired":
                raise CutoverManifestError(
                    f"cannot map legacy rows to retired account {account.id}"
                )
            elif account.venue != account_mapping.venue:
                raise CutoverManifestError(f"venue mismatch for account {account.id}")

            for user_id, role in account_mapping.memberships:
                membership = await session.scalar(
                    select(ExchangeAccountMembership).where(
                        ExchangeAccountMembership.exchange_account_id
                        == account_mapping.exchange_account_id,
                        ExchangeAccountMembership.user_id == user_id,
                    )
                )
                if membership is None:
                    session.add(
                        ExchangeAccountMembership(
                            exchange_account_id=account_mapping.exchange_account_id,
                            user_id=user_id,
                            role=role,
                        )
                    )
                else:
                    membership.role = role
            for realm in account_mapping.legacy_realms:
                mapping = await session.get(LegacyAccountRealmMap, realm)
                if mapping is None:
                    session.add(
                        LegacyAccountRealmMap(
                            realm_key=realm,
                            exchange_account_id=account_mapping.exchange_account_id,
                            source="identity-manifest",
                            manifest_sha256=manifest.sha256,
                        )
                    )
                elif mapping.exchange_account_id != account_mapping.exchange_account_id:
                    raise CutoverManifestError(f"legacy realm remapped: {realm}")
        await session.flush()

    async def _migrate_credentials(
        self, session: AsyncSession, manifest: IdentityManifest
    ) -> int:
        migrated = 0
        rows = list(await session.scalars(select(APIKey).where(APIKey.exchange_account_id.is_(None))))
        for row in rows:
            account_id = manifest.user_to_account[row.user_id]
            old_envelope = Envelope(
                secret_ciphertext=row.secret_ciphertext,
                secret_nonce=row.secret_nonce,
                wrapped_dek=row.wrapped_dek,
                dek_nonce=row.dek_nonce,
                key_version=row.key_version,
            )
            plaintext = decrypt_secret(old_envelope, user_id=row.user_id, kek=self._kek)
            new_envelope = encrypt_secret_with_aad(
                plaintext,
                aad=account_id_canonical(account_id),
                kek=self._kek,
            )
            row.secret_ciphertext = new_envelope.secret_ciphertext
            row.secret_nonce = new_envelope.secret_nonce
            row.wrapped_dek = new_envelope.wrapped_dek
            row.dek_nonce = new_envelope.dek_nonce
            row.key_version = new_envelope.key_version
            row.exchange_account_id = account_id

            credential_id = uuid5(account_id, f"legacy-api-key:{row.id}")
            target = await session.get(ExchangeAccountCredential, credential_id)
            if target is None:
                target = ExchangeAccountCredential(
                    id=credential_id,
                    exchange_account_id=account_id,
                    venue="bitfinex",
                    label=row.label,
                    api_key=row.api_key,
                    secret_ciphertext=new_envelope.secret_ciphertext,
                    secret_nonce=new_envelope.secret_nonce,
                    wrapped_dek=new_envelope.wrapped_dek,
                    dek_nonce=new_envelope.dek_nonce,
                    key_version=new_envelope.key_version,
                    lifecycle_status="active",
                    verified_at=row.verified_at,
                    last_verify_error=row.last_verify_error,
                )
                session.add(target)
            migrated += 1
        await session.flush()
        return migrated

    async def _migrate_configs(
        self, session: AsyncSession, manifest: IdentityManifest
    ) -> int:
        migrated = 0
        rows = list(await session.scalars(select(UserConfig).where(UserConfig.exchange_account_id.is_(None))))
        for row in rows:
            account_id = manifest.user_to_account[row.user_id]
            target = await session.scalar(
                select(AccountConfigDraft).where(
                    AccountConfigDraft.exchange_account_id == account_id
                )
            )
            if target is None:
                target = AccountConfigDraft(
                    id=uuid5(account_id, f"legacy-user-config:{row.id}"),
                    exchange_account_id=account_id,
                    config=dict(row.config),
                    revision=1,
                    source="user_configs",
                )
                session.add(target)
            elif target.config != row.config:
                target.config = dict(row.config)
                target.revision += 1
            row.exchange_account_id = account_id
            migrated += 1
        await session.flush()
        return migrated

    async def _backfill_money_rows(
        self, session: AsyncSession, manifest: IdentityManifest
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        for table_name, model in _MONEY_MODELS:
            count = 0
            for realm, account_id in manifest.realm_to_account.items():
                update_result = await session.execute(
                    update(model)
                    .where(model.exchange_account_id.is_(None), model.account_id == realm)
                    .values(exchange_account_id=account_id)
                )
                cursor_result = cast(CursorResult[Any], update_result)
                count += int(cursor_result.rowcount or 0)
            result[table_name] = count
            if self._failure_after_table == table_name:
                raise RuntimeError(f"injected cutover failure after {table_name}")
        await session.flush()
        return result


__all__ = [
    "AccountMapping",
    "CutoverManifestError",
    "CutoverReport",
    "CutoverVerificationError",
    "IdentityCutover",
    "IdentityCutoverError",
    "IdentityManifest",
]
