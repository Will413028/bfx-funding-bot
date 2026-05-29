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
    # venue truth dropped to 200 (S=400) -> factor 0.5
    t.reconcile_to_total(D("200"))
    assert t.deployed("a") == D("150")
    assert t.deployed("b") == D("50")


def test_reconcile_to_total_noop_when_no_intent():
    t = CellDeploymentTracker()
    # S = 0 but venue has exposure -> cannot attribute, leave per-cell at 0
    t.reconcile_to_total(D("450"))
    assert t.snapshot() == {}


def test_reconcile_to_total_scales_up():
    t = CellDeploymentTracker()
    t.record_deploy("a", D("100"))
    t.reconcile_to_total(D("150"))
    assert t.deployed("a") == D("150")
