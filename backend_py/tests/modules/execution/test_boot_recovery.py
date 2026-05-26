from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.execution.boot_recovery import (
    BootRecovery,
    LocalClaim,
    compute_recovery_actions,
    synth_orphan_cid,
    synth_orphan_scid,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

_ACC = "default"
_NOW = 2_000_000


def _offer(voi="555", amount="100", rate=0.0003, period=2):
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol="fUSD", amount=Decimal(amount),
        rate=rate, period_days=period, mts_created=1_000_000, status="ACTIVE",
    )


def _claim(cid, voi, state, size="100", scid=None, occurred=0):
    return LocalClaim(
        cid=cid, venue_offer_id=voi, state=state, size_usdt=Decimal(size),
        signal_correlation_id=scid or uuid4(), occurred_at_ms=occurred,
    )


def _actions(venue, local, grace_ms=120_000, now=_NOW):
    return compute_recovery_actions(
        venue_offers=venue, local_claims=local, account_id=_ACC,
        is_simulated=False, now_ms=now, grace_ms=grace_ms,
    )


def test_orphan_at_venue_is_claimed():
    acts = _actions([_offer(voi="555", amount="250")], [])
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationClaimed)
    assert ev.venue_offer_id == "555"
    assert ev.size_usdt == Decimal("250")
    assert ev.cid == synth_orphan_cid("555")
    assert ev.signal_correlation_id == synth_orphan_scid("555")
    assert ev.account_id == _ACC and ev.is_simulated is False


def test_local_claimed_missing_from_venue_is_released():
    scid = uuid4()
    claim = _claim(cid=42, voi="999", state=RegistryState.CLAIMED, size="80", scid=scid)
    acts = _actions([], [claim])
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationReleased)
    assert ev.cid == 42 and ev.venue_offer_id == "999"
    assert ev.size_usdt == Decimal("80") and ev.reason == "missing_from_venue"
    assert ev.signal_correlation_id == scid


def test_claimed_still_present_is_noop():
    claim = _claim(cid=42, voi="555", state=RegistryState.CLAIMED)
    assert _actions([_offer(voi="555")], [claim]) == []


def test_stale_pending_converges_to_failed():
    scid = uuid4()
    claim = _claim(cid=7, voi=None, state=RegistryState.PENDING, size="60",
                   scid=scid, occurred=_NOW - 200_000)  # older than grace
    acts = _actions([], [claim], grace_ms=120_000)
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationFailed)
    assert ev.cid == 7 and ev.size_usdt == Decimal("60")
    assert ev.reason == "unresolved_at_boot" and ev.signal_correlation_id == scid


def test_recent_pending_within_grace_is_left_alone():
    claim = _claim(cid=7, voi=None, state=RegistryState.PENDING,
                   occurred=_NOW - 1_000)  # within grace
    assert _actions([], [claim], grace_ms=120_000) == []


def test_synth_cid_numeric_voi_is_negated():
    assert synth_orphan_cid("12345") == -12345


def test_synth_cid_nonnumeric_fallback_is_deterministic_and_negative():
    from bfx_funding_bot.external.bitfinex.cid import BITFINEX_CID_MAX
    a = synth_orphan_cid("abc-xyz")
    assert a == synth_orphan_cid("abc-xyz")          # deterministic
    assert -BITFINEX_CID_MAX <= a < 0                # negative namespace, in range


def test_synth_scid_is_deterministic():
    assert synth_orphan_scid("555") == synth_orphan_scid("555")
    assert synth_orphan_scid("555") != synth_orphan_scid("556")
    assert isinstance(synth_orphan_scid("555"), UUID)


class _FailingAuthRest:
    def __init__(self, *, status_code, succeed_after=None):
        self.calls = 0
        self._status_code = status_code
        self._succeed_after = succeed_after  # if set, succeed (return []) on this call number
    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        self.calls += 1
        if self._succeed_after is not None and self.calls >= self._succeed_after:
            return []
        raise BitfinexAPIError(status_code=self._status_code, message="boom")


def _boot_recovery(auth_rest, **kw):
    return BootRecovery(
        store=None, session_factory=None, auth_rest=auth_rest,  # type: ignore[arg-type]
        account_ctx=AccountContext(
            account_id="default",
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("1"),
        ),
        deployment_environment="ci", bus=None,  # type: ignore[arg-type]
        max_attempts=3, backoff_base_s=0, **kw,
    )


def test_action_grace_skips_recent_orphan():
    # offer created 50s before now; action_grace_ms=120s → too fresh to claim
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=_NOW - 50_000, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert acts == []


def test_action_grace_skips_recent_missing_claim():
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 50_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert acts == []


def test_action_grace_releases_stale_missing_claim():
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 300_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert len(acts) == 1
    assert isinstance(acts[0], ReservationReleased)
    assert acts[0].venue_offer_id == "555"


def test_action_grace_zero_preserves_boot_behaviour():
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=_NOW - 1, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000,
    )
    assert len(acts) == 1 and isinstance(acts[0], ReservationClaimed)


@pytest.mark.asyncio
async def test_fetch_offers_does_not_retry_4xx():
    auth = _FailingAuthRest(status_code=401)
    rec = _boot_recovery(auth)
    with pytest.raises(BitfinexAPIError):
        await rec._fetch_offers()
    assert auth.calls == 1  # no retry on auth error


@pytest.mark.asyncio
async def test_fetch_offers_retries_5xx_then_succeeds():
    auth = _FailingAuthRest(status_code=503, succeed_after=3)
    rec = _boot_recovery(auth)
    result = await rec._fetch_offers()
    assert result == []
    assert auth.calls == 3  # retried twice, succeeded on 3rd


@pytest.mark.asyncio
async def test_fetch_offers_reraises_after_transient_exhaustion():
    auth = _FailingAuthRest(status_code=0)  # transport error, never succeeds
    rec = _boot_recovery(auth)
    with pytest.raises(BitfinexAPIError):
        await rec._fetch_offers()
    assert auth.calls == 3  # exhausted max_attempts
