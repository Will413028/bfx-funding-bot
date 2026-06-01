from decimal import Decimal

from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker

D = Decimal


def test_record_deploy_accumulates():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("200"))
    t.record_deploy("a", D("50"))
    assert t.deployed("a") == D("250")
    assert t.deployed("b") == D("0")


def test_snapshot_is_a_copy():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("100"))
    snap = t.snapshot()
    snap["a"] = D("999")
    assert t.deployed("a") == D("100")


def test_reconcile_to_total_scales_proportionally():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("300"))
    t.record_deploy("b", D("100"))
    # reserved dropped to 200 (S=400) -> factor 0.5 (e.g. partial release/fill)
    t.reconcile_to_total(D("200"))
    assert t.deployed("a") == D("150")
    assert t.deployed("b") == D("50")


def test_reconcile_to_total_noop_when_no_intent():
    t = CellDeploymentTracker()
    # S = 0 but venue has reserved -> cannot attribute, leave per-cell at 0
    t.reconcile_to_total(D("450"))
    assert t.snapshot() == {}


def test_reconcile_to_total_reserved_equals_intent_is_noop():
    # reserved == recorded intent -> factor == 1, no change (the normal case:
    # all our open offers are still pending, nothing filled/released yet)
    t = CellDeploymentTracker()
    t.record_deploy("a", D("100"))
    t.reconcile_to_total(D("100"))
    assert t.deployed("a") == D("100")


# ---------------------------------------------------------------------------
# C1 regression: realized (orphan) credits must NOT inflate per-cell intent
# ---------------------------------------------------------------------------

def test_realized_credits_do_not_inflate_cells():
    """Scenario: ledger shows reserved=$100, realized=$300 (orphan/pre-existing).
    reconcile_to_total receives reserved_total=$100 (NOT $400 total exposure).
    Cell intent must stay at $100, NOT jump to $400.
    This is the C1 fix regression guard.
    """
    t = CellDeploymentTracker()
    t.record_deploy("a", D("100"))
    # Only pass reserved to reconcile_to_total (not reserved+realized)
    t.reconcile_to_total(D("100"))   # reserved_total=$100; realized=$300 is NOT passed
    assert t.deployed("a") == D("100"), (
        "realized/orphan credits must not inflate cell intent (C1)"
    )


# ---------------------------------------------------------------------------
# I2: cap_per_cell clamp behavior
# ---------------------------------------------------------------------------

def test_reconcile_to_total_clamps_to_cap_per_cell():
    """After rescaling, deployed must be clamped to cap_per_cell if provided."""
    t = CellDeploymentTracker()
    t.record_deploy("a", D("500"))
    # reserved_total == sum -> factor==1, but cap_per_cell=399 should clamp
    t.reconcile_to_total(D("500"), cap_per_cell=D("399"))
    assert t.deployed("a") == D("399")


def test_reconcile_to_total_no_clamp_without_cap():
    """Without cap_per_cell, no clamping occurs."""
    t = CellDeploymentTracker()
    t.record_deploy("a", D("500"))
    t.reconcile_to_total(D("500"))
    assert t.deployed("a") == D("500")


# ---------------------------------------------------------------------------
# Phase 2: per-symbol rescale partition (cells= subset, no cross-currency contamination)
# ---------------------------------------------------------------------------

def test_rescale_is_independent_per_symbol():
    t = CellDeploymentTracker()
    t.record_deploy("fUST_a30", D("100"))
    t.record_deploy("fUST_p2", D("100"))
    t.record_deploy("fUSD_a30", D("100"))
    t.record_deploy("fUSD_p2", D("100"))
    t.reconcile_to_total(D("50"), cells=["fUST_a30", "fUST_p2"])  # only fUST sub-pool -> 25 each
    snap = t.snapshot()
    assert snap["fUST_a30"] == D("25")
    assert snap["fUST_p2"] == D("25")
    assert snap["fUSD_a30"] == D("100")  # untouched
    assert snap["fUSD_p2"] == D("100")  # untouched


def test_empty_cells_subset_is_noop():
    # cells=[] (a symbol with no cells to rescale) must rescale nothing — distinct
    # from cells=None (rescale all). Plausible Task-8 runtime state: zero active cells.
    t = CellDeploymentTracker()
    t.record_deploy("fUST_a30", D("100"))
    t.record_deploy("fUSD_a30", D("100"))
    t.reconcile_to_total(D("50"), cells=[])
    snap = t.snapshot()
    assert snap["fUST_a30"] == D("100")
    assert snap["fUSD_a30"] == D("100")


def test_unknown_cell_id_in_subset_plants_no_phantom():
    # Unknown ids contribute 0 to the sum and are skipped on write — no phantom
    # 0-entry in the snapshot.
    t = CellDeploymentTracker()
    t.record_deploy("fUST_a30", D("100"))
    t.reconcile_to_total(D("50"), cells=["fUST_a30", "ghost"])
    snap = t.snapshot()
    assert snap["fUST_a30"] == D("50")
    assert "ghost" not in snap
