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
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID, uuid5

from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import (
    Envelope,
    decrypt_secret,
    decrypt_secret_with_aad,
    encrypt_secret_with_aad,
)
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
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
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.tables import NavPeakRow
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow, ConfigRegimeRow

_MONEY_MODELS: tuple[tuple[str, type[Any]], ...] = (
    ("event_log", EventLogRow),
    ("offer_claims", OfferClaimRow),
    ("position_state", PositionStateRow),
    ("reconcile_observation", ReconcileObservationRow),
    ("execution_decisions", ExecutionDecisionRow),
    ("diagnostics", DiagnosticsRow),
    ("nav_peak", NavPeakRow),
    ("attribution_weekly", AttributionWeeklyRow),
    ("config_regime", ConfigRegimeRow),
)
_LEGACY_ZERO_ROW_MODELS: tuple[tuple[str, type[Any]], ...] = (
    ("users", User),
    ("executions", Execution),
    ("billing_records", BillingRecord),
)
_ALLOWED_WRITE_SCOPES = {"funding"}

# This is intentionally kept in application code as well as the contract
# migration.  ``verify`` must prove that the same UUID-keyed uniqueness
# constraints the DDL is about to create are already safe.
_IDENTITY_KEY_GROUPS: tuple[
    tuple[str, type[Any], tuple[str, ...], tuple[str, ...]], ...
] = (
    (
        "event_log.dedup",
        EventLogRow,
        (
            "exchange_account_id",
            "deployment_environment",
            "event_type",
            "venue_offer_id",
            "venue_seq",
        ),
        ("venue_offer_id", "venue_seq"),
    ),
    (
        "offer_claims.primary_key",
        OfferClaimRow,
        ("exchange_account_id", "deployment_environment", "cid"),
        (),
    ),
    (
        "offer_claims.venue_offer_id",
        OfferClaimRow,
        ("exchange_account_id", "deployment_environment", "venue_offer_id"),
        ("venue_offer_id",),
    ),
    (
        "offer_claims.execution_decision_id",
        OfferClaimRow,
        ("exchange_account_id", "deployment_environment", "execution_decision_id"),
        ("execution_decision_id",),
    ),
    (
        "position_state.primary_key",
        PositionStateRow,
        ("exchange_account_id", "deployment_environment", "symbol"),
        (),
    ),
    (
        "nav_peak.primary_key",
        NavPeakRow,
        ("exchange_account_id", "deployment_environment", "symbol"),
        (),
    ),
    (
        "attribution_weekly.primary_key",
        AttributionWeeklyRow,
        ("deployment_environment", "exchange_account_id", "cell", "week_start_ms"),
        (),
    ),
    (
        "config_regime.primary_key",
        ConfigRegimeRow,
        ("deployment_environment", "exchange_account_id", "recorded_at_ms"),
        (),
    ),
)


class _PermissionsClient(Protocol):
    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions: ...


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

            raw_user_ids = raw.get("user_ids")
            user_ids = (
                _string_tuple(raw_user_ids, "user_ids")
                if raw_user_ids is not None
                else ()
            )

            raw_memberships = raw.get("memberships", {})
            if not isinstance(raw_memberships, Mapping):
                raise CutoverManifestError("memberships must be an object keyed by user id")
            memberships: list[tuple[str, AccountRole]] = []
            for user_id, raw_role in raw_memberships.items():
                if not isinstance(user_id, str) or not isinstance(raw_role, str):
                    raise CutoverManifestError("membership user and role must be strings")
                if user_ids and user_id not in user_ids:
                    raise CutoverManifestError(
                        f"membership user {user_id!r} is absent from user_ids"
                    )
                memberships.append((user_id, validate_membership_role(raw_role)))
            membership_user_ids = {user_id for user_id, _role in memberships}
            if not user_ids:
                user_ids = tuple(sorted(membership_user_ids))
            if set(user_ids) != membership_user_ids:
                raise CutoverManifestError(
                    f"every user_id must have a membership for account {account_id}"
                )
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
        """Return only unambiguous mappings for one-row-per-user legacy data."""
        return {
            user_id: account_ids[0]
            for user_id, account_ids in self.user_to_accounts.items()
            if len(account_ids) == 1
        }

    @property
    def user_to_accounts(self) -> dict[str, tuple[UUID, ...]]:
        """Return the full membership multimap (a user may own many accounts)."""
        values: defaultdict[str, list[UUID]] = defaultdict(list)
        for account in self.accounts:
            for user_id in account.user_ids:
                values[user_id].append(account.exchange_account_id)
        return {
            user_id: tuple(sorted(account_ids, key=str))
            for user_id, account_ids in values.items()
        }


@dataclass(frozen=True, slots=True)
class CutoverReport:
    manifest_sha256: str
    dry_run: bool = False
    legacy_realm_counts: dict[str, int] = field(default_factory=dict)
    unmapped_rows: tuple[str, ...] = ()
    duplicate_active_credentials: tuple[str, ...] = ()
    invalid_active_credentials: tuple[str, ...] = ()
    config_conflicts: tuple[str, ...] = ()
    manifest_sha_conflicts: tuple[str, ...] = ()
    zero_row_legacy_tables: dict[str, bool] = field(default_factory=dict)
    event_head: int | None = None
    event_hash: str = ""
    backfilled_rows: dict[str, int] = field(default_factory=dict)
    migrated_credentials: int = 0
    migrated_configs: int = 0
    permissions_verified: bool = False


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
        *,
        expected_report: CutoverReport | Mapping[str, object] | None = None,
        permissions_client: _PermissionsClient | None = None,
    ) -> CutoverReport:
        normalized = _coerce_manifest(manifest)
        report = await self._preflight(session, normalized)
        self._assert_preflight_ready(report)
        self._assert_expected_evidence(report, expected_report)
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

        count_mismatches = await self._source_target_count_mismatches(session, normalized)
        if count_mismatches:
            raise CutoverVerificationError(
                "source/target count mismatch: " + "; ".join(count_mismatches)
            )

        orphan_rows = await self._orphan_exchange_account_ids(session)
        if orphan_rows:
            raise CutoverVerificationError(
                "orphan exchange_account_id: " + "; ".join(orphan_rows)
            )

        collisions = await self._identity_key_collisions(session)
        if collisions:
            raise CutoverVerificationError(
                "UUID key collisions: "
                + "; ".join(
                    f"{name}=[{', '.join(samples)}]"
                    for name, samples in sorted(collisions.items())
                )
            )

        credentials = list(
            await session.scalars(select(ExchangeAccountCredential).order_by(ExchangeAccountCredential.id))
        )
        for credential in credentials:
            self._decrypt_account_credential(credential)

        legacy_keys = list(
            await session.scalars(
                select(APIKey)
                .where(APIKey.exchange_account_id.is_not(None))
                .order_by(APIKey.id)
            )
        )
        for row in legacy_keys:
            try:
                decrypt_secret(
                    self._envelope_from_row(row), user_id=row.user_id, kek=self._kek
                )
            except InvalidTag:
                # Successful account-AAD decryption is the required proof that
                # the legacy user-bound ciphertext can no longer be replayed.
                continue
            raise CutoverVerificationError(
                f"old user AAD still decrypts for api key {row.id}"
            )

        if permissions_client is not None:
            await self._verify_credential_permissions(session, credentials, permissions_client)
            report = replace(report, permissions_verified=True)
        return report

    @staticmethod
    def _assert_expected_evidence(
        report: CutoverReport,
        expected_report: CutoverReport | Mapping[str, object] | None,
    ) -> None:
        """Require verify to consume the immutable preflight evidence file."""
        if expected_report is None:
            return
        if isinstance(expected_report, CutoverReport):
            def get_value(key: str) -> object:
                return getattr(expected_report, key)
        else:
            required = (
                "manifest_sha256",
                "event_head",
                "event_hash",
                "legacy_realm_counts",
            )
            missing = [key for key in required if key not in expected_report]
            if missing:
                raise CutoverVerificationError(
                    "verification evidence missing: " + ", ".join(missing)
                )
            def get_value(key: str) -> object:
                return expected_report.get(key)

        expected_manifest = get_value("manifest_sha256")
        if expected_manifest != report.manifest_sha256:
            raise CutoverVerificationError(
                f"manifest SHA mismatch: expected {expected_manifest}, "
                f"observed {report.manifest_sha256}"
            )
        expected_head = get_value("event_head")
        if expected_head != report.event_head:
            raise CutoverVerificationError(
                f"event head mismatch: expected {expected_head}, observed {report.event_head}"
            )
        expected_hash = get_value("event_hash")
        if expected_hash != report.event_hash:
            raise CutoverVerificationError(
                f"event hash mismatch: expected {expected_hash}, observed {report.event_hash}"
            )
        raw_counts = get_value("legacy_realm_counts")
        if not isinstance(raw_counts, Mapping):
            raise CutoverVerificationError("verification evidence legacy_realm_counts must be an object")
        try:
            expected_counts = {str(key): int(value) for key, value in raw_counts.items()}
        except (TypeError, ValueError) as exc:
            raise CutoverVerificationError(
                "verification evidence legacy_realm_counts must contain integers"
            ) from exc
        if expected_counts != report.legacy_realm_counts:
            raise CutoverVerificationError(
                "legacy realm count mismatch: "
                f"expected {expected_counts}, observed {report.legacy_realm_counts}"
            )

    @staticmethod
    async def _source_target_count_mismatches(
        session: AsyncSession, manifest: IdentityManifest
    ) -> tuple[str, ...]:
        """Compare every migrated source row with its account-owned target."""
        mismatches: list[str] = []
        for account in manifest.accounts:
            account_id = account.exchange_account_id
            for table_name, model in _MONEY_MODELS:
                source_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.account_id.in_(account.legacy_realms))
                    )
                    or 0
                )
                target_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.exchange_account_id == account_id)
                    )
                    or 0
                )
                if source_count != target_count:
                    mismatches.append(
                        f"{table_name}:{account_id}:source={source_count},target={target_count}"
                    )

            source_key_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(APIKey)
                    .where(APIKey.user_id.in_(account.user_ids))
                )
                or 0
            )
            target_key_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ExchangeAccountCredential)
                    .where(
                        ExchangeAccountCredential.exchange_account_id == account_id,
                        ExchangeAccountCredential.venue == "bitfinex",
                    )
                )
                or 0
            )
            if source_key_count != target_key_count:
                mismatches.append(
                    f"api_keys:{account_id}:source={source_key_count},target={target_key_count}"
                )

            source_configs = list(
                await session.scalars(
                    select(UserConfig.config).where(
                        UserConfig.user_id.in_(account.user_ids)
                    )
                )
            )
            # The target is deliberately one draft per account.  Multiple
            # legacy users may carry the exact same preference; preflight
            # already rejects divergent configs, so compare presence rather
            # than raw source row count after deterministic deduplication.
            source_config_count = 1 if source_configs else 0
            if len(
                {
                    json.dumps(config, sort_keys=True, separators=(",", ":"))
                    for config in source_configs
                }
            ) > 1:
                mismatches.append(
                    f"user_configs:{account_id}:source configs diverge"
                )
            target_config_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AccountConfigDraft)
                    .where(AccountConfigDraft.exchange_account_id == account_id)
                )
                or 0
            )
            if source_config_count != target_config_count:
                mismatches.append(
                    f"user_configs:{account_id}:source={source_config_count},target={target_config_count}"
                )
        return tuple(sorted(mismatches))

    @staticmethod
    async def _orphan_exchange_account_ids(session: AsyncSession) -> tuple[str, ...]:
        known_ids = set(await session.scalars(select(ExchangeAccount.id)))
        orphans: set[str] = set()
        for table_name, model in _MONEY_MODELS:
            values = await session.scalars(
                select(model.exchange_account_id)
                .where(model.exchange_account_id.is_not(None))
                .distinct()
            )
            for value in values:
                if value not in known_ids:
                    orphans.add(f"{table_name}:{value}")
        for table_name, model in (("api_keys", APIKey), ("user_configs", UserConfig)):
            values = await session.scalars(
                select(model.exchange_account_id)
                .where(model.exchange_account_id.is_not(None))
                .distinct()
            )
            for value in values:
                if value not in known_ids:
                    orphans.add(f"{table_name}:{value}")
        return tuple(sorted(orphans))

    @staticmethod
    async def _identity_key_collisions(
        session: AsyncSession,
    ) -> dict[str, tuple[str, ...]]:
        collisions: dict[str, tuple[str, ...]] = {}
        for name, model, column_names, non_null_columns in _IDENTITY_KEY_GROUPS:
            columns = tuple(getattr(model, column_name) for column_name in column_names)
            predicates = [model.exchange_account_id.is_not(None)]
            predicates.extend(
                getattr(model, column_name).is_not(None)
                for column_name in non_null_columns
            )
            result = await session.execute(
                select(*columns, func.count().label("duplicate_count"))
                .where(*predicates)
                .group_by(*columns)
                .having(func.count() > 1)
                .order_by(*columns)
                .limit(20)
            )
            samples: list[str] = []
            for row in result:
                values = [
                    f"{column_name}={row[index]!s}"
                    for index, column_name in enumerate(column_names)
                ]
                values.append(f"count={row[len(column_names)]}")
                samples.append(",".join(values))
            if samples:
                collisions[name] = tuple(samples)
        return collisions

    @staticmethod
    def _envelope_from_row(row: Any) -> Envelope:
        return Envelope(
            secret_ciphertext=row.secret_ciphertext,
            secret_nonce=row.secret_nonce,
            wrapped_dek=row.wrapped_dek,
            dek_nonce=row.dek_nonce,
            key_version=row.key_version,
        )

    def _decrypt_account_credential(self, credential: ExchangeAccountCredential) -> str:
        try:
            return decrypt_secret_with_aad(
                self._envelope_from_row(credential),
                aad=account_id_canonical(credential.exchange_account_id),
                kek=self._kek,
            )
        except InvalidTag as exc:
            raise CutoverVerificationError(
                f"credential {credential.id} cannot decrypt with account UUID AAD"
            ) from exc

    async def _verify_credential_permissions(
        self,
        session: AsyncSession,
        credentials: Sequence[ExchangeAccountCredential],
        client: _PermissionsClient,
    ) -> None:
        del session  # retained in the signature for parity with vault verification
        for credential in credentials:
            if credential.lifecycle_status == "retired":
                continue
            secret = self._decrypt_account_credential(credential)
            context = AccountContext(
                account_id=account_id_canonical(credential.exchange_account_id),
                credentials=Credentials(api_key=credential.api_key, api_secret=secret),
                allocation_cap_usdt=Decimal("0"),
            )
            try:
                permissions = await client.get_key_permissions(ctx=context)
            except BitfinexAPIError as exc:
                raise CutoverVerificationError(
                    f"credential permission verification failed for {credential.id}"
                ) from exc
            if not permissions.can("funding", write=True):
                raise CutoverVerificationError(
                    f"credential {credential.id} lacks funding write permission"
                )
            offending = sorted(
                scope
                for scope, (_read, write) in permissions.scopes.items()
                if write and scope not in _ALLOWED_WRITE_SCOPES
            )
            if offending:
                raise CutoverVerificationError(
                    f"credential {credential.id} has unsafe write scopes: "
                    + ",".join(offending)
                )

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
                account_ids = manifest.user_to_accounts.get(user_id, ())
                if not account_ids:
                    unmapped.add(f"{table_name}:user:{user_id}")
                elif len(account_ids) > 1:
                    unmapped.add(
                        f"{table_name}:user:{user_id}:ambiguous-accounts"
                    )

        duplicate_credentials = await self._duplicate_credential_accounts(session, manifest)
        config_conflicts = await self._config_conflicts(session, manifest)
        manifest_sha_conflicts = await self._manifest_sha_conflicts(session, manifest)
        invalid_active_credentials = tuple(sorted(
            str(row.id)
            for row in await session.scalars(
                select(ExchangeAccountCredential).where(
                    ExchangeAccountCredential.lifecycle_status == "active",
                    (
                        ExchangeAccountCredential.verified_at.is_(None)
                        | ExchangeAccountCredential.last_verify_error.is_not(None)
                    ),
                )
            )
        ))
        zero_row_legacy_tables = {
            table_name: bool(
                (await session.scalar(select(func.count()).select_from(model))) == 0
            )
            for table_name, model in _LEGACY_ZERO_ROW_MODELS
        }
        # Halt 1 runs at the additive identity revision. Select only columns
        # present at that revision: EventLogRow also maps event-v3 columns
        # introduced after the identity contract migration.
        events = list(
            (
                await session.execute(
                    select(
                        EventLogRow.event_seq,
                        EventLogRow.account_id,
                        EventLogRow.exchange_account_id,
                        EventLogRow.deployment_environment,
                        EventLogRow.event_type,
                        EventLogRow.cid,
                        EventLogRow.venue_offer_id,
                        EventLogRow.venue_seq,
                        EventLogRow.payload,
                        EventLogRow.occurred_at_ms,
                    ).order_by(EventLogRow.event_seq.asc())
                )
            ).mappings()
        )
        event_payload = [
            {
                "event_seq": row["event_seq"],
                "account_id": row["account_id"],
                # Include the resolved UUID in the evidence even before the
                # backfill.  This makes the preflight hash identity-aware and
                # lets verify prove that the owner did not change in transit.
                "exchange_account_id": str(
                    row["exchange_account_id"]
                    or realm_to_account.get(row["account_id"])
                )
                if (
                    row["exchange_account_id"]
                    or realm_to_account.get(row["account_id"])
                )
                else None,
                "deployment_environment": row["deployment_environment"],
                "event_type": row["event_type"],
                "cid": row["cid"],
                "venue_offer_id": row["venue_offer_id"],
                "venue_seq": row["venue_seq"],
                "payload": row["payload"],
                "occurred_at_ms": row["occurred_at_ms"],
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
            invalid_active_credentials=invalid_active_credentials,
            config_conflicts=config_conflicts,
            manifest_sha_conflicts=manifest_sha_conflicts,
            zero_row_legacy_tables=zero_row_legacy_tables,
            event_head=events[-1]["event_seq"] if events else None,
            event_hash=hashlib.sha256(encoded_events).hexdigest(),
        )

    @staticmethod
    async def _config_conflicts(
        session: AsyncSession, manifest: IdentityManifest
    ) -> tuple[str, ...]:
        grouped: defaultdict[UUID, list[tuple[str, str]]] = defaultdict(list)
        rows = await session.scalars(
            select(UserConfig).where(UserConfig.exchange_account_id.is_(None))
        )
        for row in rows:
            account_id = manifest.user_to_account.get(row.user_id)
            if account_id is not None:
                encoded = json.dumps(row.config, sort_keys=True, separators=(",", ":"))
                grouped[account_id].append((row.user_id, encoded))
        # A partially completed cutover may already have a target draft.  It
        # is safe to reuse it only when every legacy source config agrees with
        # it; otherwise refusing is safer than silently choosing a winner.
        for account_id in grouped:
            target = await session.scalar(
                select(AccountConfigDraft).where(
                    AccountConfigDraft.exchange_account_id == account_id
                )
            )
            if target is not None:
                grouped[account_id].append(
                    ("<account-config-draft>", json.dumps(target.config, sort_keys=True, separators=(",", ":")))
                )
        conflicts: list[str] = []
        for account_id, values in grouped.items():
            distinct_configs = {encoded for _user_id, encoded in values}
            if len(distinct_configs) > 1:
                users = ",".join(sorted(user_id for user_id, _encoded in values))
                conflicts.append(f"{account_id}:users={users}")
        return tuple(sorted(conflicts))

    @staticmethod
    async def _manifest_sha_conflicts(
        session: AsyncSession, manifest: IdentityManifest
    ) -> tuple[str, ...]:
        conflicts: list[str] = []
        for realm, account_id in sorted(manifest.realm_to_account.items()):
            mapping = await session.get(LegacyAccountRealmMap, realm)
            if mapping is None:
                continue
            if mapping.exchange_account_id != account_id:
                conflicts.append(f"{realm}:account-remapped")
            elif mapping.manifest_sha256 != manifest.sha256:
                conflicts.append(f"{realm}:manifest-sha-mismatch")
        return tuple(conflicts)

    @staticmethod
    async def _duplicate_credential_accounts(
        session: AsyncSession, manifest: IdentityManifest
    ) -> set[str]:
        counts: defaultdict[UUID, int] = defaultdict(int)
        rows = await session.scalars(
            select(APIKey).where(
                APIKey.exchange_account_id.is_(None),
                APIKey.exchange_status == "verified",
                APIKey.verified_at.is_not(None),
                APIKey.last_verify_error.is_(None),
            )
        )
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
        if report.invalid_active_credentials:
            raise CutoverManifestError(
                "active credentials lack successful verification: "
                + ", ".join(report.invalid_active_credentials)
            )
        if report.config_conflicts:
            raise CutoverManifestError(
                "config conflicts for account: " + ", ".join(report.config_conflicts)
            )
        if report.manifest_sha_conflicts:
            raise CutoverManifestError(
                "manifest SHA mismatch: " + ", ".join(report.manifest_sha_conflicts)
            )
        nonzero_legacy_tables = sorted(
            table_name
            for table_name, is_zero in report.zero_row_legacy_tables.items()
            if not is_zero
        )
        if nonzero_legacy_tables:
            raise CutoverManifestError(
                "legacy tables must be empty: " + ", ".join(nonzero_legacy_tables)
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
                elif mapping.manifest_sha256 != manifest.sha256:
                    raise CutoverManifestError(f"manifest SHA mismatch: {realm}")
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
            source_verified = (
                row.exchange_status == "verified"
                and row.verified_at is not None
                and row.last_verify_error is None
            )
            target_status = "active" if source_verified else "pending"
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
                    lifecycle_status=target_status,
                    verified_at=row.verified_at if source_verified else None,
                    last_verify_error=row.last_verify_error,
                )
                session.add(target)
            elif target.lifecycle_status == "active" and (
                target.verified_at is None or target.last_verify_error is not None
            ):
                # Repair rows created by an interrupted/older cutover image;
                # never leave an unverifiable credential executable.
                target.lifecycle_status = "pending"
                target.verified_at = None
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
