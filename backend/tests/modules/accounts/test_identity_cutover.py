from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.crypto import (
    Envelope,
    decrypt_secret,
    decrypt_secret_with_aad,
    encrypt_secret,
)
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.modules.accounts.config_service import (
    get_account_config_draft,
    upsert_account_config_draft_for_user,
)
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    MembershipDenied,
    grant_membership,
)
from bfx_funding_bot.modules.accounts.identity_cutover import (
    CutoverManifestError,
    CutoverVerificationError,
    IdentityCutover,
    IdentityManifest,
)
from bfx_funding_bot.modules.accounts.tables import (
    AccountConfigDraft,
    APIKey,
    ExchangeAccount,
    ExchangeAccountCredential,
    LegacyAccountRealmMap,
    User,
    UserConfig,
)
from bfx_funding_bot.modules.accounts.vault import (
    create_account_credential,
    delete_account_credential,
    list_account_credentials,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

_KEK = bytes(range(32))
_ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_LEGACY_USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


def _manifest(*, realm_key: str = "default") -> IdentityManifest:
    return IdentityManifest.from_dict(
        {
            "version": 1,
            "accounts": [
                {
                    "exchange_account_id": str(_ACCOUNT_ID),
                    "venue": "bitfinex",
                    "label": "Primary",
                    "legacy_realms": [realm_key],
                    "user_ids": ["operator-1"],
                    "memberships": {"operator-1": "owner"},
                }
            ],
        }
    )


def _multi_account_manifest() -> IdentityManifest:
    return IdentityManifest.from_dict(
        {
            "version": 1,
            "accounts": [
                {
                    "exchange_account_id": str(_ACCOUNT_ID),
                    "venue": "bitfinex",
                    "label": "Primary",
                    "legacy_realms": ["default"],
                    "user_ids": ["operator-1"],
                    "memberships": {"operator-1": "owner"},
                },
                {
                    "exchange_account_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "venue": "bitfinex",
                    "label": "Secondary",
                    "legacy_realms": ["secondary"],
                    "user_ids": ["operator-1"],
                    "memberships": {"operator-1": "operator"},
                },
            ],
        }
    )


async def _seed_legacy_rows(session: AsyncSession) -> Envelope:
    envelope = encrypt_secret("legacy-secret", user_id="operator-1", kek=_KEK)
    session.add(
        APIKey(
            user_id="operator-1",
            label="main",
            api_key="PUB",
            secret_ciphertext=envelope.secret_ciphertext,
            secret_nonce=envelope.secret_nonce,
            wrapped_dek=envelope.wrapped_dek,
            dek_nonce=envelope.dek_nonce,
            key_version=envelope.key_version,
        )
    )
    session.add(UserConfig(user_id="operator-1", config={"period_days": 2}))
    session.add(
        EventLogRow(
            account_id="default",
            deployment_environment="shadow",
            event_type="ReservationIntent",
            cid=1,
            venue_offer_id=None,
            venue_seq=None,
            payload={"account_id": "default"},
            occurred_at_ms=1000,
        )
    )
    await session.flush()
    return envelope


@pytest.mark.asyncio
async def test_dry_run_does_not_write_and_apply_is_idempotent(session: AsyncSession) -> None:
    await _seed_legacy_rows(session)
    cutover = IdentityCutover(kek=_KEK)
    manifest = _manifest()

    report = await cutover.apply(session, manifest, dry_run=True)
    assert report.dry_run is True
    assert await session.scalar(select(func.count()).select_from(ExchangeAccount)) == 0
    assert await session.scalar(select(func.count()).select_from(ExchangeAccountCredential)) == 0

    first = await cutover.apply(session, manifest)
    await session.commit()
    counts_after_first = (
        await session.scalar(select(func.count()).select_from(ExchangeAccount)),
        await session.scalar(select(func.count()).select_from(ExchangeAccountCredential)),
        await session.scalar(select(func.count()).select_from(AccountConfigDraft)),
    )

    second = await cutover.apply(session, manifest)
    await session.commit()
    counts_after_second = (
        await session.scalar(select(func.count()).select_from(ExchangeAccount)),
        await session.scalar(select(func.count()).select_from(ExchangeAccountCredential)),
        await session.scalar(select(func.count()).select_from(AccountConfigDraft)),
    )

    assert first.manifest_sha256 == second.manifest_sha256 == manifest.sha256
    assert counts_after_first == counts_after_second == (1, 1, 1)


def test_manifest_supports_one_user_memberships_on_multiple_accounts() -> None:
    manifest = _multi_account_manifest()
    assert len(manifest.accounts) == 2
    assert manifest.user_to_accounts["operator-1"] == (
        _ACCOUNT_ID,
        UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
    )


def test_manifest_rejects_partial_membership_declaration() -> None:
    with pytest.raises(CutoverManifestError, match="every user_id must have a membership"):
        IdentityManifest.from_dict(
            {
                "version": 1,
                "accounts": [
                    {
                        "exchange_account_id": str(_ACCOUNT_ID),
                        "venue": "bitfinex",
                        "label": "Primary",
                        "legacy_realms": ["default"],
                        "user_ids": ["owner", "operator"],
                        "memberships": {"owner": "owner"},
                    }
                ],
            }
        )


def test_manifest_derives_user_ids_from_memberships() -> None:
    manifest = IdentityManifest.from_dict(
        {
            "version": 1,
            "accounts": [
                {
                    "exchange_account_id": str(_ACCOUNT_ID),
                    "venue": "bitfinex",
                    "label": "Primary",
                    "legacy_realms": ["default"],
                    "memberships": {"operator-1": "owner"},
                }
            ],
        }
    )
    assert manifest.accounts[0].user_ids == ("operator-1",)


@pytest.mark.asyncio
async def test_wrong_manifest_fails_before_any_write(session: AsyncSession) -> None:
    await _seed_legacy_rows(session)

    with pytest.raises(CutoverManifestError, match="unmapped"):
        await IdentityCutover(kek=_KEK).apply(session, _manifest(realm_key="wrong"))

    assert await session.scalar(select(func.count()).select_from(ExchangeAccount)) == 0
    row = await session.scalar(select(EventLogRow))
    assert row is not None
    assert row.exchange_account_id is None


@pytest.mark.asyncio
async def test_config_conflict_blocks_cutover_before_writes(session: AsyncSession) -> None:
    session.add_all(
        [
            UserConfig(user_id="operator-1", config={"period_days": 2}),
            UserConfig(user_id="operator-2", config={"period_days": 7}),
        ]
    )
    await session.flush()
    manifest = IdentityManifest.from_dict(
        {
            "version": 1,
            "accounts": [
                {
                    "exchange_account_id": str(_ACCOUNT_ID),
                    "venue": "bitfinex",
                    "label": "Primary",
                    "legacy_realms": ["default"],
                    "user_ids": ["operator-1", "operator-2"],
                    "memberships": {"operator-1": "owner", "operator-2": "operator"},
                }
            ],
        }
    )

    with pytest.raises(CutoverManifestError, match="config conflicts"):
        await IdentityCutover(kek=_KEK).apply(session, manifest)

    assert await session.scalar(select(func.count()).select_from(ExchangeAccount)) == 0


@pytest.mark.asyncio
async def test_identical_multi_user_configs_deduplicate_to_one_account_draft(
    session: AsyncSession,
) -> None:
    session.add_all(
        [
            UserConfig(user_id="operator-1", config={"period_days": 2}),
            UserConfig(user_id="operator-2", config={"period_days": 2}),
        ]
    )
    await session.flush()
    manifest = IdentityManifest.from_dict(
        {
            "version": 1,
            "accounts": [
                {
                    "exchange_account_id": str(_ACCOUNT_ID),
                    "venue": "bitfinex",
                    "label": "Primary",
                    "legacy_realms": ["default"],
                    "user_ids": ["operator-1", "operator-2"],
                    "memberships": {
                        "operator-1": "owner",
                        "operator-2": "operator",
                    },
                }
            ],
        }
    )

    await IdentityCutover(kek=_KEK).apply(session, manifest)
    await session.commit()

    assert await session.scalar(select(func.count()).select_from(AccountConfigDraft)) == 1
    report = await IdentityCutover(kek=_KEK).verify(session, manifest)
    assert report.migrated_configs == 0


@pytest.mark.asyncio
async def test_multiple_unverified_legacy_credentials_stage_as_pending(
    session: AsyncSession,
) -> None:
    first = encrypt_secret("secret-1", user_id="operator-1", kek=_KEK)
    second = encrypt_secret("secret-2", user_id="operator-2", kek=_KEK)
    session.add_all(
        [
            APIKey(
                user_id="operator-1",
                label="one",
                api_key="PUB1",
                secret_ciphertext=first.secret_ciphertext,
                secret_nonce=first.secret_nonce,
                wrapped_dek=first.wrapped_dek,
                dek_nonce=first.dek_nonce,
                key_version=first.key_version,
            ),
            APIKey(
                user_id="operator-2",
                label="two",
                api_key="PUB2",
                secret_ciphertext=second.secret_ciphertext,
                secret_nonce=second.secret_nonce,
                wrapped_dek=second.wrapped_dek,
                dek_nonce=second.dek_nonce,
                key_version=second.key_version,
            ),
        ]
    )
    await session.flush()
    manifest = IdentityManifest.from_dict(
        {
            "version": 1,
            "accounts": [
                {
                    "exchange_account_id": str(_ACCOUNT_ID),
                    "venue": "bitfinex",
                    "label": "Primary",
                    "legacy_realms": ["default"],
                    "user_ids": ["operator-1", "operator-2"],
                    "memberships": {
                        "operator-1": "owner",
                        "operator-2": "operator",
                    },
                }
            ],
        }
    )

    await IdentityCutover(kek=_KEK).apply(session, manifest)
    await session.commit()

    statuses = list(
        await session.scalars(
            select(ExchangeAccountCredential.lifecycle_status).order_by(
                ExchangeAccountCredential.id
            )
        )
    )
    assert statuses == ["pending", "pending"]


@pytest.mark.asyncio
async def test_manifest_sha_change_is_rejected_for_existing_realm_map(
    session: AsyncSession,
) -> None:
    manifest = _manifest()
    session.add(
        ExchangeAccount(
            id=_ACCOUNT_ID,
            venue="bitfinex",
            label="Primary",
            lifecycle_status="active",
        )
    )
    session.add(
        LegacyAccountRealmMap(
            realm_key="default",
            exchange_account_id=_ACCOUNT_ID,
            source="identity-manifest",
            manifest_sha256="different-manifest-sha",
        )
    )
    await session.flush()

    with pytest.raises(CutoverManifestError, match="manifest SHA mismatch"):
        await IdentityCutover(kek=_KEK).apply(session, manifest)


@pytest.mark.asyncio
async def test_nonzero_legacy_scaffold_blocks_cutover_before_writes(
    session: AsyncSession,
) -> None:
    session.add(
        User(
            id=_LEGACY_USER_ID,
            email="legacy@test.invalid",
            password_hash="redacted",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.flush()

    with pytest.raises(CutoverManifestError, match="legacy tables must be empty"):
        await IdentityCutover(kek=_KEK).apply(session, _manifest())

    assert await session.scalar(select(func.count()).select_from(ExchangeAccount)) == 0
    assert await session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio
async def test_credentials_are_reencrypted_with_account_aad(session: AsyncSession) -> None:
    old_envelope = await _seed_legacy_rows(session)
    assert decrypt_secret(old_envelope, user_id="operator-1", kek=_KEK) == "legacy-secret"

    await IdentityCutover(kek=_KEK).apply(session, _manifest())
    await session.commit()

    legacy = await session.scalar(select(APIKey).where(APIKey.user_id == "operator-1"))
    target = await session.scalar(select(ExchangeAccountCredential))
    assert legacy is not None and target is not None
    converted = Envelope(
        secret_ciphertext=target.secret_ciphertext,
        secret_nonce=target.secret_nonce,
        wrapped_dek=target.wrapped_dek,
        dek_nonce=target.dek_nonce,
        key_version=target.key_version,
    )
    with pytest.raises(InvalidTag):
        decrypt_secret(converted, user_id="operator-1", kek=_KEK)
    assert decrypt_secret_with_aad(
        converted, aad=str(_ACCOUNT_ID), kek=_KEK
    ) == "legacy-secret"
    assert legacy.exchange_account_id == _ACCOUNT_ID


@pytest.mark.asyncio
async def test_unverified_legacy_credential_is_migrated_pending(session: AsyncSession) -> None:
    await _seed_legacy_rows(session)

    await IdentityCutover(kek=_KEK).apply(session, _manifest())
    await session.commit()

    target = await session.scalar(select(ExchangeAccountCredential))
    assert target is not None
    assert target.lifecycle_status == "pending"
    assert target.verified_at is None


@pytest.mark.asyncio
async def test_verify_rejects_ciphertext_still_bound_to_legacy_user_aad(
    session: AsyncSession,
) -> None:
    await _seed_legacy_rows(session)
    manifest = _manifest()
    cutover = IdentityCutover(kek=_KEK)
    await cutover.apply(session, manifest)
    await session.commit()

    legacy = await session.scalar(select(APIKey).where(APIKey.user_id == "operator-1"))
    assert legacy is not None
    old_envelope = encrypt_secret("still-legacy", user_id="operator-1", kek=_KEK)
    legacy.secret_ciphertext = old_envelope.secret_ciphertext
    legacy.secret_nonce = old_envelope.secret_nonce
    legacy.wrapped_dek = old_envelope.wrapped_dek
    legacy.dek_nonce = old_envelope.dek_nonce
    legacy.key_version = old_envelope.key_version
    await session.flush()

    with pytest.raises(CutoverVerificationError, match="old user AAD"):
        await cutover.verify(session, manifest)


@pytest.mark.asyncio
async def test_verify_requires_matching_preflight_evidence(session: AsyncSession) -> None:
    await _seed_legacy_rows(session)
    manifest = _manifest()
    cutover = IdentityCutover(kek=_KEK)
    preflight = await cutover.preflight(session, manifest)
    await cutover.apply(session, manifest)
    await session.commit()

    with pytest.raises(CutoverVerificationError, match="event hash mismatch"):
        await cutover.verify(
            session,
            manifest,
            expected_report={
                "manifest_sha256": manifest.sha256,
                "event_head": preflight.event_head,
                "event_hash": "wrong-hash",
                "legacy_realm_counts": preflight.legacy_realm_counts,
            },
        )


@pytest.mark.asyncio
async def test_verify_rejects_source_target_count_mismatch(session: AsyncSession) -> None:
    await _seed_legacy_rows(session)
    manifest = _manifest()
    cutover = IdentityCutover(kek=_KEK)
    await cutover.apply(session, manifest)
    target = await session.scalar(select(AccountConfigDraft))
    assert target is not None
    await session.delete(target)
    await session.commit()

    with pytest.raises(CutoverVerificationError, match="source/target count mismatch"):
        await cutover.verify(session, manifest)


class _PermissionClient:
    def __init__(self, permissions: KeyPermissions) -> None:
        self.permissions = permissions
        self.account_ids: list[str] = []

    async def get_key_permissions(self, *, ctx) -> KeyPermissions:
        self.account_ids.append(ctx.account_id)
        return self.permissions


@pytest.mark.asyncio
async def test_verify_permission_gate_checks_all_nonretired_credentials(
    session: AsyncSession,
) -> None:
    await _seed_legacy_rows(session)
    manifest = _manifest()
    cutover = IdentityCutover(kek=_KEK)
    await cutover.apply(session, manifest)
    await session.commit()

    client = _PermissionClient(
        KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, False)})
    )
    report = await cutover.verify(session, manifest, permissions_client=client)
    assert report.permissions_verified is True
    assert client.account_ids == [str(_ACCOUNT_ID)]


@pytest.mark.asyncio
async def test_verify_permission_gate_rejects_unsafe_write_scope(session: AsyncSession) -> None:
    await _seed_legacy_rows(session)
    manifest = _manifest()
    cutover = IdentityCutover(kek=_KEK)
    await cutover.apply(session, manifest)
    await session.commit()

    client = _PermissionClient(
        KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, True)})
    )
    with pytest.raises(CutoverVerificationError, match="unsafe write scopes"):
        await cutover.verify(session, manifest, permissions_client=client)


@pytest.mark.asyncio
async def test_partial_failure_rolls_back_all_backfills(session: AsyncSession) -> None:
    await _seed_legacy_rows(session)

    with pytest.raises(RuntimeError, match="injected cutover failure"):
        await IdentityCutover(kek=_KEK, failure_after_table="event_log").apply(
            session, _manifest()
        )

    assert await session.scalar(select(func.count()).select_from(ExchangeAccount)) == 0
    row = await session.scalar(select(EventLogRow))
    assert row is not None
    assert row.exchange_account_id is None


@pytest.mark.asyncio
async def test_account_scoped_vault_and_config_require_membership(session: AsyncSession) -> None:
    account = ExchangeAccount(
        id=_ACCOUNT_ID,
        venue="bitfinex",
        label="Primary",
        lifecycle_status="active",
    )
    session.add(account)
    await session.flush()
    await grant_membership(
        session,
        exchange_account_id=_ACCOUNT_ID,
        user_id="operator-1",
        role="owner",
    )
    await grant_membership(
        session,
        exchange_account_id=_ACCOUNT_ID,
        user_id="viewer-1",
        role="viewer",
    )

    credential = await create_account_credential(
        session,
        exchange_account_id=_ACCOUNT_ID,
        user_id="operator-1",
        label="main",
        api_key="PUB",
        api_secret="secret",
        kek=_KEK,
    )
    assert len(
        await list_account_credentials(
            session, exchange_account_id=_ACCOUNT_ID, user_id="viewer-1"
        )
    ) == 1
    await upsert_account_config_draft_for_user(
        session,
        exchange_account_id=_ACCOUNT_ID,
        user_id="operator-1",
        config={"period_days": 2},
    )
    assert await get_account_config_draft(
        session, exchange_account_id=_ACCOUNT_ID, user_id="viewer-1"
    ) is not None
    with pytest.raises(MembershipDenied):
        await upsert_account_config_draft_for_user(
            session,
            exchange_account_id=_ACCOUNT_ID,
            user_id="viewer-1",
            config={"period_days": 7},
        )
    assert await delete_account_credential(
        session,
        exchange_account_id=_ACCOUNT_ID,
        user_id="operator-1",
        key_id=credential.id,
    ) is True
    assert credential.lifecycle_status == "retired"
