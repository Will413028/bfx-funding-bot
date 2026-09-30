"""Pure resolution proof, independent of projections and legacy store imports."""

from collections.abc import Mapping
from typing import Any

from bfx_funding_bot.core.venue_time import VENUE_CLOCK_TOLERANCE_MS
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.trading_shadow._internal.evidence import (
    Event,
    Row,
    amount,
    full_coverage,
    require,
)


def _flags(value: object) -> object:
    require(
        isinstance(value, (Mapping, int)) and not isinstance(value, bool),
        "unknown_match_evidence_gap",
    )
    return dict(value) if isinstance(value, Mapping) else {"raw": value}


def _candidates(attempt: SubmissionAttemptPayload, snapshot: Row) -> list[Row]:
    # unknown_matching.py:282-361,76-153: exact/zero requires complete,
    # bounded offer history, including empty history; active rows win duplicates.
    c = snapshot["coverage"]
    start, end = c.get("offer_history_start_ms"), c.get("offer_history_end_ms")
    pages = c.get("offer_history_pages", 0)
    oldest, newest = c.get("offer_history_oldest_mts"), c.get("offer_history_newest_mts")
    require(
        type(start) is int
        and type(end) is int
        and type(pages) is int
        and start <= attempt.started_at_ms <= end
        and end >= snapshot["query_started_at_ms"]
        and c.get("offer_history_complete") is True
        and pages >= 1
        and (
            (oldest is None and newest is None)
            or (type(oldest) is int and type(newest) is int and oldest <= newest)
        ),
        "unknown_match_evidence_gap",
    )
    by_id: dict[str, tuple[Row, bool, tuple[object, ...]]] = {}
    for key in ("offer_history", "offers"):
        require(isinstance(snapshot.get(key), list), "unknown_match_evidence_gap")
        for offer in snapshot[key]:
            if not isinstance(offer, Mapping) or offer.get("symbol") != attempt.symbol:
                continue
            require(
                isinstance(offer.get("venue_offer_id"), str)
                and bool(offer["venue_offer_id"])
                and type(offer.get("period_days")) is int
                and type(offer.get("mts_created")) is int
                and isinstance(offer.get("offer_type"), str)
                and bool(offer["offer_type"])
                and isinstance(offer.get("status"), str),
                "unknown_match_evidence_gap",
            )
            amount(offer["amount_remaining"])
            identity = (
                offer["symbol"],
                amount(offer["amount_original"]),
                amount(offer["rate"]),
                offer["period_days"],
                offer["mts_created"],
                offer["offer_type"],
                _flags(offer["flags"]),
            )
            prior = by_id.get(offer["venue_offer_id"])
            require(prior is None or prior[2] == identity, "unknown_match_evidence_gap")
            by_id[offer["venue_offer_id"]] = (offer, key == "offer_history", identity)
    wire = attempt.normalized_payload
    require(
        type(wire["period"]) in {int, str} and int(wire["period"]) > 0, "unknown_match_evidence_gap"
    )
    wanted = (attempt.symbol, amount(wire["amount"]), amount(wire["rate"]), int(wire["period"]))
    return [
        offer
        for offer, history, identity in by_id.values()
        if identity[:4] == wanted
        and attempt.started_at_ms - VENUE_CLOCK_TOLERANCE_MS <= offer["mts_created"] <= end
        and identity[5:] == (wire["type"], _flags(wire["flags"]))
        and (
            not history
            or (
                oldest is not None
                and newest is not None
                and oldest <= offer["mts_created"] <= newest
            )
        )
    ]


def prove_resolution(
    event: Event,
    opening: Event,
    reconcile: Event,
    latest_reconcile_seq: int,
    attempt: SubmissionAttemptPayload | None,
) -> None:
    p, snapshot = event.payload, reconcile.payload
    # store.py:644-653 proves an earlier referenced reconcile for automatic
    # matches. Only manual resolutions additionally require the latest fence
    # and a query begun after opening (770-855); do not conflate these paths.
    require(
        0 < reconcile.seq < event.seq
        and reconcile.kind == "VENUE_SNAPSHOT_OBSERVED"
        and type(snapshot.get("query_started_at_ms")) is int
        and type(snapshot.get("query_finished_at_ms")) is int
        and snapshot["query_started_at_ms"] <= snapshot["query_finished_at_ms"]
        and full_coverage(snapshot),
        "unknown_match_evidence_gap",
        seq=event.seq,
    )
    if event.kind != "SUBMIT_MATCHED_TO_VENUE_OFFER":
        require(
            opening.seq < reconcile.seq
            and reconcile.seq == latest_reconcile_seq
            and opening.occurred_at_ms < snapshot["query_started_at_ms"],
            "unknown_match_evidence_gap",
            seq=event.seq,
        )
    expected: dict[str, Any] = {
        "reconcile_event_seq": reconcile.seq,
        "query_started_at_ms": snapshot["query_started_at_ms"],
        "query_finished_at_ms": snapshot["query_finished_at_ms"],
    }
    if event.kind == "UNCERTAINTY_MANUALLY_RESOLVED":
        require(
            p["kind"] in {"unattributed_venue_offer", "unsupported_venue_exposure"}
            and p["resolution_action"] in {"accepted_external_exposure", "closed_at_venue"},
            "unknown_match_evidence_gap",
        )
    else:
        require(attempt is not None, "unknown_match_evidence_gap")
        assert attempt is not None
        candidates = _candidates(attempt, snapshot)
        if event.kind == "UNCERTAINTY_MARKED_NOT_ACCEPTED":
            require(
                p["kind"] == "submit_outcome_unknown"
                and p["candidate_count"] == 0
                and not candidates,
                "unknown_match_evidence_gap",
            )
            expected["candidate_count"] = 0
        else:
            # store.py:644-653,697-711,872-895: never trust the resolution label alone.
            require(len(candidates) == 1, "unknown_match_evidence_gap")
            matched = candidates[0]
            require(
                matched["venue_offer_id"] == p["venue_offer_id"]
                and matched["status"] == p["venue_status"]
                and matched.get("cid") in {None, attempt.cid}
                and matched.get("execution_decision_id") in {None, attempt.execution_decision_id},
                "unknown_match_evidence_gap",
            )
            if event.kind == "SUBMIT_MATCHED_TO_VENUE_OFFER":
                require(
                    p["matched_mts_created"] == matched["mts_created"], "unknown_match_evidence_gap"
                )
                return
            require(p["kind"] == "submit_outcome_unknown", "unknown_match_evidence_gap")
            expected.update(candidate_count=1, venue_offer_id=p["venue_offer_id"])
    # store.py:885-941 / unknown_matching.py:390-409: server-derived exact evidence.
    require(p["resolution_evidence"] == expected, "unknown_match_evidence_gap")
    require(
        bool(p["resolved_by_operator_id"].strip()) and bool(p["resolution_reason"].strip()),
        "unknown_match_evidence_gap",
    )
