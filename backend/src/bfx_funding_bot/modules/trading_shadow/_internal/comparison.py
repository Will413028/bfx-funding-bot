"""Non-authorizing comparison of two observations in one caller-owned snapshot."""

import json
from hashlib import sha256
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.capital_shadow_port import (
    BaselineAvailable,
    BaselineBlocked,
    BaselineNotComparable,
)
from bfx_funding_bot.modules.trading import (
    AppliedPolicy,
    Available,
    Blocked,
    CapitalResult,
    CapitalScope,
    CapitalView,
    DifferenceClassification,
    derive_capital,
)
from bfx_funding_bot.modules.trading_shadow.contracts import (
    BaselineReader,
    CandidateReader,
    ComparisonHeads,
    FieldDifference,
    LoadedInputs,
    ShadowComparison,
    canonical_bytes,
)


def _result_fields(result: CapitalResult) -> dict[str, object]:
    if isinstance(result, Blocked):
        return {"kind": "blocked", "reason": result.reason}
    view = result.view
    result_fields: dict[str, object] = {"kind": "available"}
    for name in ("account_id", "environment", "symbol", "revision", "digest", "revision_id"):
        result_fields[f"applied.{name}"] = getattr(view.applied, name)
    result_fields["applied.policy"] = view.applied.policy
    # capital_snapshots.query_id is unique: the query identifies the accepted
    # snapshot on both arms. The legacy arm has no observation identity.
    result_fields["query_id"] = view.query_id
    for name in ("available_amount", "unreflected_commitments", "total_capital", "cell_exposure"):
        result_fields[f"snapshot.{name}"] = getattr(view.snapshot, name)
    for name in ("spendable", "cell_limit", "cell_headroom", "max_new_offer", "reason"):
        result_fields[f"budget.{name}"] = getattr(view.budget, name)
    result_fields["unattributed_credit_exposure"] = view.unattributed_credit_exposure
    return result_fields


def _compare(candidate: CapitalResult, baseline: CapitalResult) -> tuple[FieldDifference, ...]:
    left, right = _result_fields(candidate), _result_fields(baseline)
    return tuple(
        FieldDifference(
            path, json.loads(canonical_bytes(left.get(path))),
            json.loads(canonical_bytes(right.get(path))),
        )
        for path in sorted(left.keys() | right.keys())
        if left.get(path) != right.get(path)
    )


def _classifications(
    candidate: CapitalResult, loaded: LoadedInputs, differences: tuple[FieldDifference, ...]
) -> tuple[DifferenceClassification, ...]:
    labels: set[DifferenceClassification] = set()
    if isinstance(candidate, Blocked):
        if candidate.reason == "snapshot_query_pending":
            labels.add("query_pending")
        if candidate.reason == "execution_unknown":
            accepted = loaded.inputs.accepted
            if any(
                u.is_open and u.symbol == loaded.inputs.scope.symbol
                for u in loaded.inputs.uncertainties
            ):
                labels.add("unknown_open")
            elif any(
                symbol == loaded.inputs.scope.symbol
                for _, symbol in (*accepted.unresolved_attempts, *accepted.unresolved_quarantines)
            ):
                labels.add("resolved_waiting_snapshot")
    # Both arms read one snapshot, so a value difference is never an expected
    # transient; it stays unclassified for evidence review instead of being
    # labelled by whichever scenario happens to be present.
    return tuple(sorted(labels))


def _mapped_baseline(value: BaselineAvailable | BaselineBlocked, loaded: LoadedInputs) -> CapitalResult:
    if isinstance(value, BaselineBlocked):
        return Blocked(value.reason, ())
    applied = AppliedPolicy(
        value.account_id, value.environment, value.symbol, value.revision,
        value.digest, value.revision_id, value.policy,
    )
    # The legacy view exposes raw classification JSON and no observation
    # identity. Its common acceptance identity is the query (compared in the
    # result) and the fence (compared below); the observation and attribution
    # fields are only typed carriers and are never compared.
    return Available(CapitalView(
        applied, value.query_id, loaded.inputs.accepted.observation_id, value.snapshot,
        value.budget, value.unattributed_credit_exposure, loaded.inputs.accepted,
    ))


async def compare_capital(
    session: AsyncSession, *, scope: CapitalScope, now_ms: int, max_snapshot_age_ms: int,
    candidate_reader: CandidateReader, baseline_reader: BaselineReader,
) -> ShadowComparison:
    """Catch unexpected failures at the outer boundary, recording only their type."""
    candidate: CapitalResult | None = None
    baseline: CapitalResult | None = None
    candidate_digest: str | None = None
    observation_digest: str | None = None
    heads = ComparisonHeads()

    def result(
        status: Literal["equal", "different", "not_comparable", "error"], *,
        differences: tuple[FieldDifference, ...] = (),
        classifications: tuple[DifferenceClassification, ...] = (), reason: str | None = None,
        evidence: tuple[tuple[str, str], ...] = (),
    ) -> ShadowComparison:
        return ShadowComparison(
            "fold_comparison", status, candidate, baseline, differences, classifications,
            candidate_digest, None, False, observation_digest, heads, reason, evidence,
        )

    try:
        loaded = await candidate_reader.load(
            session, scope=scope, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms,
        )
        if not isinstance(loaded, LoadedInputs):
            return result(
                "not_comparable", classifications=(loaded.classification,),
                reason=loaded.reason, evidence=loaded.evidence,
            )
        candidate_digest = loaded.candidate_input_digest
        candidate = derive_capital(
            scope=scope, policy=loaded.inputs.policy, accepted=loaded.inputs.accepted,
            attempts=loaded.inputs.attempts, uncertainties=loaded.inputs.uncertainties,
            read_context=loaded.inputs.read_context,
        )
        observed = await baseline_reader(
            session, scope=scope, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms,
        )
        heads = ComparisonHeads(observed.watermark, observed.projection_cursor)
        if isinstance(observed, BaselineNotComparable):
            return result(
                "not_comparable", classifications=("projection_integrity",), reason=observed.reason,
            )
        baseline = _mapped_baseline(observed, loaded)
        observation_digest = sha256(canonical_bytes({
            "watermark": observed.watermark,
            "projection_cursor": observed.projection_cursor,
            "policy": (
                (observed.account_id, observed.environment, observed.symbol,
                 observed.revision, observed.digest, observed.revision_id)
                if isinstance(observed, BaselineAvailable) else None
            ),
            "accepted": (
                (observed.snapshot_seq, observed.command_fence, observed.query_id)
                if isinstance(observed, BaselineAvailable) else None
            ),
            "scope": scope,
            "now_ms": now_ms,
            "max_snapshot_age_ms": max_snapshot_age_ms,
            "result": _result_fields(baseline),
        })).hexdigest()
        differences = _compare(candidate, baseline)
        if isinstance(observed, BaselineAvailable) and isinstance(candidate, Available):
            # The query is compared in the result; the S0 high water is the
            # legacy command fence it was mapped from.
            left, right = loaded.inputs.accepted.attempt_seq_high_water, observed.command_fence
            if left != right:
                differences += (FieldDifference(
                    "attribution.command_fence", json.loads(canonical_bytes(left)),
                    json.loads(canonical_bytes(right)),
                ),)
        return result(
            "different" if differences else "equal", differences=differences,
            classifications=_classifications(candidate, loaded, differences),
        )
    except Exception as exc:
        return result("error", reason=type(exc).__name__)
