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

