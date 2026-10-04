"""``deploy/vm/shadow.env`` is the live profile on the simulated venue, and only that.

Mutations (one at a time; revert after each): add a legacy or secret key to ``shadow.env``
(``test_the_soak_profile_carries_no_legacy_secret_or_identity_key``); change a shared key such
as ``BFX_REPRICE_MIN_AGE_S`` (``test_shadow_differs_from_live_only_by_the_allowed_keys``); widen
``ONLY_DIFFERENT_VALUE`` or ``LIVE_ONLY`` here to cover the change (the allowlist is the contract).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bfx_funding_bot.apps.config import load_config
from bfx_funding_bot.modules.simulated_venue import SimulatedVenueConfig
from tests.modules.marketfeed.test_deploy_profile import read_env

ROOT = Path(__file__).resolve().parents[3]
SHADOW = ROOT / "deploy/vm/shadow.env"
LIVE = ROOT / "deploy/vm/live.env"

# Keys both profiles name whose value is deliberately different for the soak.
ONLY_DIFFERENT_VALUE = {
    "BFX_PHASE": ("live", "shadow"),
    "BFX_DEPLOYMENT_ENV": ("prod", "shadow"),
    "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": ("15", "60"),
    "BFX_OTEL_ENABLED": ("true", "false"),
    "BFX_BOOK_SNAPSHOT_ENABLED": ("true", "false"),
}
# Keys only the live profile names: the auth WS is Bitfinex's (the registry refuses it for the
# simulated venue) and the snapshot interval means nothing with the snapshot writer off.
LIVE_ONLY = {"BFX_WS_CLIENT_ENABLED", "BFX_BOOK_SNAPSHOT_INTERVAL_S"}
SHADOW_ONLY: set[str] = set()

FORBIDDEN = {
    # legacy sizing / executor / account knobs (LEGACY_ENV of tests/integration/sim_daemon.py)
    "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
    "BFX_VENUE_FLOOR_USD", "BFX_MIN_OFFER_BUFFER_PCT", "BFX_EXECUTOR", "BFX_ACCOUNT_ID",
    "BFX_WS_CLIENT_ENABLED", "BFX_FILL_TRACKER_ENABLED",
    # secrets and identities: the protected sim.env, never a tracked file
    "BFX_VAULT_KEK", "BFX_ADMIN_TOKEN", "BFX_EXCHANGE_ACCOUNT_ID", "BFX_OPERATOR_USER_ID",
    "DATABASE_URL", "BFX_DEPLOYMENT_ID", "BFX_KILL_SWITCH",
    # per-run soak settings
    "BFX_SIM_INITIAL_WALLETS", "BFX_SIM_FAULTS",
}


def test_shadow_differs_from_live_only_by_the_allowed_keys() -> None:
    shadow, live = read_env(SHADOW), read_env(LIVE)
    assert set(live) - set(shadow) == LIVE_ONLY
    assert set(shadow) - set(live) == SHADOW_ONLY
    changed = {k: (live[k], shadow[k]) for k in set(live) & set(shadow) if live[k] != shadow[k]}
    assert changed == ONLY_DIFFERENT_VALUE


def test_the_soak_profile_carries_no_legacy_secret_or_identity_key() -> None:
    names = set(read_env(SHADOW))
    assert not names & FORBIDDEN
    assert not {n for n in names if n.startswith(("TELEGRAM", "BFX_TELEGRAM", "BFX_CANARY_"))}


def test_the_book_reconcile_interval_stays_below_the_venues_maximum_book_age() -> None:
    interval = float(read_env(SHADOW)["BFX_BOOK_RECONCILE_INTERVAL_SECONDS"])
    venue_max_age_s = SimulatedVenueConfig("k", "s").max_book_age_ms / 1000
    assert interval < venue_max_age_s


def test_the_soak_profile_loads_as_the_simulated_venue_on_the_live_cells(
        monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in read_env(SHADOW).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture:fixture@localhost/fixture")
    monkeypatch.delenv("BFX_SIM_FAULTS", raising=False)
    monkeypatch.delenv("BFX_SIM_INITIAL_WALLETS", raising=False)
    config = load_config(cells_yaml_path=ROOT / "backend/configs/cells.live.yaml")
    assert config.venue == "simulated" and config.phase.value == "shadow"
    assert config.deployment_environment.value == "shadow"
    assert [cell.cell_id for cell in config.cells] == ["fUST_a30", "fUST_p2"]
    assert config.book_reconcile_interval_seconds == 60
    assert config.simulated_faults == {} and config.simulated_initial_wallets == {}
