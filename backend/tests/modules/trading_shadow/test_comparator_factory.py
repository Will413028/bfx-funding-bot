"""Public composition preserves structured evidence without baseline coupling."""

from unittest.mock import AsyncMock
from uuid import UUID

from bfx_funding_bot.modules.trading import CapitalScope
from bfx_funding_bot.modules.trading_shadow import NotComparable, wiring


async def test_factory_binds_loader_and_retains_not_comparable_evidence(monkeypatch):
    evidence = (("table", "capital_policy_heads"), ("key", "missing"))
    loader = AsyncMock()
    loader.load.return_value = NotComparable("policy_missing", evidence)
    monkeypatch.setattr(wiring, "build_candidate_loader", lambda **kw: loader)
    baseline = AsyncMock()
    comparator = wiring.build_capital_comparator(source_revision="test", baseline_reader=baseline)
    result = await comparator(AsyncMock(), scope=CapitalScope(UUID(int=1), "ci", "fUST", "fUST_a30"),
                              now_ms=2000, max_snapshot_age_ms=10000)
    assert result.status == "not_comparable"
    assert result.reason == "policy_missing"
    assert result.evidence == evidence
    baseline.assert_not_called()
