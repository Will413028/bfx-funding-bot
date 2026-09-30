"""Comparison status and field contract without a database."""

from dataclasses import replace
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.capital_shadow_port import (
    BaselineAvailable,
    BaselineBlocked,
    BaselineNotComparable,
)
from bfx_funding_bot.modules.trading import Available, Blocked
from bfx_funding_bot.modules.trading_shadow import (
    CandidateInputs,
    InputManifest,
    LoadedInputs,
    NotComparable,
    ScanLimits,
)
from bfx_funding_bot.modules.trading_shadow._internal.comparison import compare_capital
from tests.modules.trading.test_capital import BASIS, CONTEXT, POLICY, SCOPE, fold


def loaded(*, attempts=(), context=CONTEXT):
    inputs = CandidateInputs(SCOPE, POLICY, BASIS, attempts, (), context)
    manifest = InputManifest(
        "unit", SCOPE, 1200, 500, ScanLimits(), (), b"{}", b"{}", b"{}", (), "facts",
    )
    return LoadedInputs(inputs, manifest, "candidate-digest")


def baseline_for(candidate):
    assert isinstance(candidate, Available)
    view = candidate.view
    return BaselineAvailable(
        SCOPE.account_id, SCOPE.environment, SCOPE.symbol,
        POLICY.revision, POLICY.digest, POLICY.revision_id, POLICY.policy,
        20, BASIS.attempt_seq_high_water, BASIS.query_id,
        view.snapshot, view.budget, view.unattributed_credit_exposure, 30, 30,
    )


class FakeCandidate:
    def __init__(self, value):
        self.value = value

    async def load(self, session, **kwargs):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeBaseline:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def __call__(self, session, **kwargs):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


async def compare(candidate, baseline):
    return await compare_capital(
        object(), scope=SCOPE, now_ms=1200, max_snapshot_age_ms=500,
        candidate_reader=FakeCandidate(candidate), baseline_reader=FakeBaseline(baseline),
    )


@pytest.mark.asyncio
async def test_equal_observation_digest():
    entry = loaded()
    result = await compare(entry, baseline_for(fold()))
    assert result.status == "equal"
    assert result.differences == ()
    assert result.baseline_input_digest is None
    assert result.baseline_evidence_complete is False
    assert result.baseline_observation_digest is not None
    assert result.candidate_input_digest == "candidate-digest"
    assert result.heads.watermark == result.heads.projection_cursor == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("available_amount", "snapshot.available_amount"),
        ("unreflected_commitments", "snapshot.unreflected_commitments"),
        ("total_capital", "snapshot.total_capital"),
        ("cell_exposure", "snapshot.cell_exposure"),
        ("spendable", "budget.spendable"),
        ("cell_limit", "budget.cell_limit"),
        ("cell_headroom", "budget.cell_headroom"),
        ("max_new_offer", "budget.max_new_offer"),
        ("unattributed_credit_exposure", "unattributed_credit_exposure"),
    ],
)
async def test_every_decimal_is_compared(field, path):
    base = baseline_for(fold())
    if field == "unattributed_credit_exposure":
        changed = replace(base, unattributed_credit_exposure=Decimal("51"))
    elif hasattr(base.snapshot, field):
        changed = replace(
            base, snapshot=replace(base.snapshot, **{field: getattr(base.snapshot, field) + 1})
        )
    else:
        changed = replace(base, budget=replace(base.budget, **{field: Decimal("1")}))
    result = await compare(loaded(), changed)
    assert result.status == "different"
    assert path in {difference.path for difference in result.differences}


@pytest.mark.asyncio
async def test_acceptance_identity_and_blocked_reason():
    base = baseline_for(fold())
    changed = await compare(loaded(), replace(base, command_fence=11))
    assert {d.path for d in changed.differences} == {"attribution.command_fence"}
    blocked = await compare(loaded(context=replace(CONTEXT, latest_query_id=BASIS.query_id)),
                            BaselineBlocked("execution_unknown", 30, 30))
    assert blocked.status == "different"
    assert any(d.path == "kind" for d in blocked.differences)
    assert isinstance(blocked.baseline, Blocked)


@pytest.mark.asyncio
async def test_accepted_query_identity_is_compared():
    base = baseline_for(fold())
    changed = await compare(loaded(), replace(base, query_id=POLICY.revision_id))
    assert changed.status == "different"
    assert {d.path for d in changed.differences} == {"query_id"}


@pytest.mark.asyncio
async def test_blocked_reasons_and_available_budget_reason():
    pending = loaded(context=replace(CONTEXT, latest_query_id=POLICY.revision_id))
    equal = await compare(pending, BaselineBlocked("snapshot_query_pending", 30, 30))
    assert equal.status == "equal" and equal.classifications == ("query_pending",)
    changed = await compare(pending, BaselineBlocked("execution_unknown", 30, 30))
    assert changed.status == "different"
    assert {d.path for d in changed.differences} == {"reason"}
    base = baseline_for(fold())
    budget = await compare(loaded(), replace(base, budget=replace(base.budget, reason="policy_disabled")))
    assert budget.status == "different"
    assert isinstance(budget.baseline, Available)
    assert {d.path for d in budget.differences} == {"budget.reason"}


@pytest.mark.asyncio
async def test_not_comparable_and_error_never_equal():
    baseline = FakeBaseline(baseline_for(fold()))
    missing = await compare_capital(
        object(), scope=SCOPE, now_ms=1200, max_snapshot_age_ms=500,
        candidate_reader=FakeCandidate(NotComparable("history_limit")), baseline_reader=baseline,
    )
    assert missing.status == "not_comparable" and baseline.calls == 0
    lag = await compare(loaded(), BaselineNotComparable("projection_cursor_lag", 29, 30))
    assert lag.status == "not_comparable" and lag.classifications == ("projection_integrity",)
    for fault in (FakeCandidate(RuntimeError("secret payload")), FakeBaseline(RuntimeError("secret payload"))):
        result = await compare_capital(
            object(), scope=SCOPE, now_ms=1200, max_snapshot_age_ms=500,
            candidate_reader=fault if isinstance(fault, FakeCandidate) else FakeCandidate(loaded()),
            baseline_reader=fault if isinstance(fault, FakeBaseline) else baseline,
        )
        assert result.status == "error" and result.reason == "RuntimeError"
        assert "secret" not in repr(result)
