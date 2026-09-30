"""Candidate SQL reader: one caller-owned PostgreSQL RR READ ONLY snapshot.

Never opens/commits transactions, flushes, locks, replays, or consults mutable
projections. The caller owns statement_timeout and transaction lifetime.
"""

from decimal import getcontext
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.trading import CapitalReadContext, CapitalScope
from bfx_funding_bot.modules.trading_shadow._internal.evidence import (
    EvidenceError,
    Row,
    decode_basis,
    decode_event,
    decode_policy,
    integrity_block,
    require,
)
from bfx_funding_bot.modules.trading_shadow._internal.facts import decode_facts
from bfx_funding_bot.modules.trading_shadow.contracts import (
    CandidateInputs,
    InputManifest,
    LoadedInputs,
    LoadResult,
    NotComparable,
    ScanLimits,
    candidate_input_digest,
    canonical_bytes,
)

_SCOPE = "exchange_account_id = :account AND deployment_environment = :environment"
_EVENT_COLUMNS = (
    "event_seq, account_id, exchange_account_id, deployment_environment, event_type, "
    "cid, venue_offer_id, venue_seq, event_id, schema_version, payload, occurred_at_ms, "
    "octet_length(CAST(payload AS text)) AS payload_bytes"
)
_QUERY_COLUMNS = (
    "id, exchange_account_id, deployment_environment, command_fence, query_revision, started_at_ms"
)
_UNCERTAINTY_TYPES = (
    "SUBMIT_OUTCOME_UNKNOWN",
    "VENUE_OFFER_QUARANTINED",
    "SUBMIT_MATCHED_TO_VENUE_OFFER",
    "UNCERTAINTY_BOUND_TO_VENUE_OFFER",
    "UNCERTAINTY_MARKED_NOT_ACCEPTED",
    "UNCERTAINTY_MANUALLY_RESOLVED",
)
_TAIL_TYPES = (
    "RESERVATION_INTENT",
    "RESERVATION_CLAIMED",
    "RESERVATION_FAILED",
    *_UNCERTAINTY_TYPES[0:1],
    *_UNCERTAINTY_TYPES[2:],
)
_RESOLUTION_TYPES = _UNCERTAINTY_TYPES[2:]


async def _rows(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[Row]:
    return cast(list[Row], list((await session.execute(text(sql), params)).mappings()))


async def _event_set(
    session: AsyncSession,
    *,
    params: dict[str, Any],
    kinds: tuple[str, ...],
    lower_bound: int | None,
    limits: ScanLimits,
) -> tuple[list[Row], int]:
    """Bound each complete typed set before transferring any JSON payload."""
    names = ", ".join(f":kind_{i}" for i in range(len(kinds)))
    typed_params = {
        **params,
        **{f"kind_{i}": kind for i, kind in enumerate(kinds)},
        "row_limit": limits.max_history_rows + 1,
    }
    if lower_bound is not None:
        typed_params["fence"] = lower_bound
    predicate = f"{_SCOPE} AND event_seq <= :watermark AND event_type IN ({names})"
    if lower_bound is not None:
        predicate += " AND event_seq > :fence"
    sizes = await _rows(
        session,
        "SELECT event_seq, octet_length(CAST(payload AS text)) AS payload_bytes "
        f"FROM event_log WHERE {predicate} ORDER BY event_seq LIMIT :row_limit",
        typed_params,
    )
    require(
        len(sizes) <= limits.max_history_rows,
        "history_row_limit",
        rows=len(sizes),
        limit=limits.max_history_rows,
    )
    payload_bytes = sum(row["payload_bytes"] for row in sizes)
    require(
        payload_bytes <= limits.max_payload_bytes,
        "history_payload_limit",
        bytes=payload_bytes,
        limit=limits.max_payload_bytes,
    )
    rows = await _rows(
        session,
        f"SELECT {_EVENT_COLUMNS} FROM event_log WHERE {predicate} "
        "ORDER BY event_seq LIMIT :row_limit",
        typed_params,
    )
    require(
        [row["event_seq"] for row in rows] == [row["event_seq"] for row in sizes],
        "input_evidence_gap",
    )
    require(
        [row["payload_bytes"] for row in rows] == [row["payload_bytes"] for row in sizes],
        "input_evidence_gap",
    )
    return rows, payload_bytes


async def _one(session: AsyncSession, sql: str, params: dict[str, Any], reason: str) -> Row:
    rows = await _rows(session, sql, params)
    require(len(rows) == 1, reason)
    return rows[0]


class CandidateLoader:
    def __init__(self, *, limits: ScanLimits, source_revision: str) -> None:
        if not source_revision.strip():
            raise ValueError("source_revision is required")
        self.limits = limits
        self.source_revision = source_revision

    async def load(
        self, session: AsyncSession, *, scope: CapitalScope, now_ms: int, max_snapshot_age_ms: int
    ) -> LoadResult:
        """Return proven inputs or an eligibility failure; unexpected DB errors propagate.

        No incomplete bundle is called comparable. Missing policy/basis cannot
        be represented by made-up fold inputs. Existing reason codes and S0's
        input_evidence_gap classification describe evidence failures.
        """
        try:
            require(
                session.in_transaction() and not (session.new or session.dirty or session.deleted),
                "input_evidence_gap",
                transaction="caller_owned_clean_transaction_required",
            )
            require(
                type(now_ms) is int
                and now_ms >= 0
                and type(max_snapshot_age_ms) is int
                and max_snapshot_age_ms >= 0,
                "input_evidence_gap",
                clock="invalid",
            )
            with session.no_autoflush:
                settings = await _one(
                    session,
                    "SELECT current_setting('transaction_isolation') AS isolation, "
                    "current_setting('transaction_read_only') AS read_only",
                    {},
                    "input_evidence_gap",
                )
                require(
                    settings["isolation"] == "repeatable read" and settings["read_only"] == "on",
                    "input_evidence_gap",
                    transaction="REPEATABLE READ READ ONLY required",
                )
                return await self._load(session, scope, now_ms, max_snapshot_age_ms)
        except EvidenceError as exc:
            return NotComparable(exc.reason, exc.evidence)
        # Anything else (malformed payload shapes included) propagates: the comparator
        # reports it as status=error, never as a not_comparable evidence gap.

    async def _load(
        self, session: AsyncSession, scope: CapitalScope, now_ms: int, max_snapshot_age_ms: int
    ) -> LoadedInputs:
        params: dict[str, Any] = {
            "account": scope.account_id,
            "environment": scope.environment,
            "symbol": scope.symbol,
        }
        watermark = int(
            await session.scalar(
                text(f"SELECT COALESCE(MAX(event_seq), 0) FROM event_log WHERE {_SCOPE}"), params
            )
            or 0
        )
        params["watermark"] = watermark
        head = await _one(
            session,
            f"SELECT exchange_account_id, deployment_environment, symbol, revision_id, revision "
            f"FROM capital_policy_heads WHERE {_SCOPE} AND symbol = :symbol",
            params,
            "policy_unavailable",
        )
        params["revision_id"] = head["revision_id"]
        policy_row = await _one(
            session,
            "SELECT id, exchange_account_id, deployment_environment, symbol, revision, "
            "schema_version, policy, digest FROM capital_policy_revisions WHERE id = :revision_id",
            params,
            "inconsistent_policy_pointer",
        )
        policy = decode_policy(scope, head, policy_row)
        accepted_row = await _one(
            session,
            "SELECT event_seq, query_id, exchange_account_id, deployment_environment, schema_version, "
            "command_fence, classification, covered_prefix_hash, authorization_blocked_reason "
            f"FROM capital_snapshots WHERE {_SCOPE} AND event_seq <= :watermark "
            "ORDER BY event_seq DESC LIMIT 1",
            params,
            "snapshot_unavailable",
        )
        latest_query = await _one(
            session,
            f"SELECT {_QUERY_COLUMNS} FROM capital_snapshot_queries WHERE {_SCOPE} "
            "ORDER BY query_revision DESC LIMIT 1",
            params,
            "snapshot_evidence_missing",
        )
        params["query_id"] = accepted_row["query_id"]
        query = await _one(
            session,
            f"SELECT {_QUERY_COLUMNS} FROM capital_snapshot_queries WHERE {_SCOPE} AND id = :query_id",
            params,
            "snapshot_evidence_missing",
        )
        params["accepted_seq"] = accepted_row["event_seq"]
        prefixes = await _rows(
            session,
            f"SELECT event_seq, prefix_hash FROM event_prefix_hashes WHERE {_SCOPE} "
            "AND event_seq IN (:watermark, :accepted_seq) ORDER BY event_seq",
            params,
        )
        prefix_by_seq = {row["event_seq"]: row["prefix_hash"] for row in prefixes}
        require(watermark in prefix_by_seq, "snapshot_prefix_diverged", seq=watermark)

        params["fence"] = accepted_row["command_fence"]
        uncertainty_rows, uncertainty_bytes = await _event_set(
            session, params=params, kinds=_UNCERTAINTY_TYPES, lower_bound=None, limits=self.limits
        )
        tail_rows, tail_bytes = await _event_set(
            session,
            params=params,
            kinds=_TAIL_TYPES,
            lower_bound=accepted_row["command_fence"],
            limits=self.limits,
        )
        payload_bytes = uncertainty_bytes + tail_bytes
        require(
            payload_bytes <= self.limits.max_payload_bytes,
            "history_payload_limit",
            bytes=payload_bytes,
            limit=self.limits.max_payload_bytes,
        )

        reference_lookups = 0
        point_rows: dict[int, Row] = {}

        async def point(
            sql: str, point_params: dict[str, Any], reason: str, *, optional: bool = False
        ) -> Row | None:
            nonlocal reference_lookups, payload_bytes
            reference_lookups += 1
            require(
                reference_lookups <= self.limits.max_reference_lookups,
                "reference_lookup_limit",
                lookups=reference_lookups,
            )
            found = await _rows(session, sql, point_params)
            require(len(found) <= 1 and (optional or len(found) == 1), reason)
            if not found:
                return None
            row = found[0]
            if "payload" in row:
                payload_bytes += row["payload_bytes"]
                require(
                    payload_bytes <= self.limits.max_payload_bytes,
                    "history_payload_limit",
                    bytes=payload_bytes,
                    limit=self.limits.max_payload_bytes,
                )
                point_rows[row["event_seq"]] = row
            return row

        accepted_event_row = await point(
            f"SELECT {_EVENT_COLUMNS} FROM event_log WHERE {_SCOPE} "
            "AND event_seq = :accepted_seq AND event_seq <= :watermark",
            params,
            "snapshot_evidence_missing",
        )
        assert accepted_event_row is not None
        accepted_event = decode_event(accepted_event_row, scope, watermark)
        accepted, confirmation_block = decode_basis(
            scope,
            accepted_row,
            query,
            accepted_event,
            prefix_by_seq.get(accepted_row["event_seq"]),
            watermark,
        )
        # Historical uncertainty may reference an intent before F. A missing
        # intent is valid for legacy UNKNOWN; one point query per decision proves it.
        tail_seqs = {row["event_seq"] for row in tail_rows}
        historical_decisions = sorted(
            {
                str(row["payload"]["reservation_ref"]["execution_decision_id"])
                for row in uncertainty_rows
                if row["payload"].get("reservation_ref") is not None
                and row["event_seq"] not in tail_seqs
            }
        )
        for decision_id in historical_decisions:
            intent_row = await point(
                f"SELECT {_EVENT_COLUMNS} FROM event_log WHERE {_SCOPE} "
                "AND event_type = 'RESERVATION_INTENT' AND event_seq <= :watermark "
                "AND payload->>'execution_decision_id' = :decision_id "
                "ORDER BY event_seq LIMIT 2",
                {**params, "decision_id": decision_id},
                "attempt_intent_conflict",
                optional=True,
            )
            if intent_row is not None:
                require(
                    intent_row["event_seq"]
                    < min(
                        row["event_seq"]
                        for row in uncertainty_rows
                        if row["payload"].get("reservation_ref", {}).get("execution_decision_id")
                        == decision_id
                    ),
                    "attempt_intent_conflict",
                )

        resolution_rows = [
            row
            for _, row in sorted(
                {
                    row["event_seq"]: row
                    for row in (*uncertainty_rows, *tail_rows)
                    if row["event_type"] in _RESOLUTION_TYPES
                }.items()
            )
        ]
        latest_reconcile_by_resolution: dict[int, int] = {}
        seq_only_rows: list[Row] = []
        for row in resolution_rows:
            reconcile_seq = row["payload"]["reconcile_event_seq"]
            if reconcile_seq not in point_rows:
                await point(
                    f"SELECT {_EVENT_COLUMNS} FROM event_log WHERE {_SCOPE} "
                    "AND event_seq = :reconcile_seq AND event_seq <= :watermark",
                    {**params, "reconcile_seq": reconcile_seq},
                    "unknown_match_evidence_gap",
                )
            latest = await _rows(
                session,
                "SELECT event_seq FROM event_log WHERE "
                + _SCOPE
                + " AND event_type = 'VENUE_SNAPSHOT_OBSERVED' "
                "AND event_seq < :resolution_seq AND event_seq <= :watermark "
                "ORDER BY event_seq DESC LIMIT 1",
                {**params, "resolution_seq": row["event_seq"]},
            )
            seq_only_rows.extend(latest)
            latest_reconcile_by_resolution[row["event_seq"]] = (
                latest[0]["event_seq"] if latest else 0
            )
        latest_observation_rows = await _rows(
            session,
            "SELECT event_seq FROM event_log WHERE "
            + _SCOPE
            + " AND event_type = 'VENUE_SNAPSHOT_OBSERVED' AND event_seq <= :watermark "
            "ORDER BY event_seq DESC LIMIT 1",
            params,
        )
        seq_only_rows.extend(latest_observation_rows)
        latest_observation = (
            latest_observation_rows[0]["event_seq"] if latest_observation_rows else 0
        )

        all_rows = {
            row["event_seq"]: row for row in (*uncertainty_rows, *tail_rows, *point_rows.values())
        }
        events = tuple(decode_event(row, scope, watermark) for _, row in sorted(all_rows.items()))
        decision_ids = sorted(
            {
                event.payload["submission_attempt"]["execution_decision_id"]
                for event in events
                if event.kind == "RESERVATION_INTENT"
                and event.payload.get("submission_attempt") is not None
            }
        )
        decisions: dict[str, Row] = {}
        for decision_id in decision_ids:
            params["decision_id"] = decision_id
            decision = await point(
                "SELECT decision_id, exchange_account_id, deployment_environment, cell_id, symbol, "
                "signal_correlation_id, amount_usdt FROM execution_decisions "
                f"WHERE {_SCOPE} AND decision_id = :decision_id",
                params,
                "attempt_decision_conflict",
            )
            assert decision is not None
            decisions[decision_id] = decision
        attempts, uncertainties, _ = decode_facts(
            scope,
            events,
            decisions,
            accepted.attempt_seq_high_water,
            self.limits.max_reference_lookups,
            latest_reconcile_by_resolution,
        )
        inputs = CandidateInputs(
            scope,
            policy,
            accepted,
            attempts,
            uncertainties,
            CapitalReadContext(
                now_ms,
                max_snapshot_age_ms,
                latest_query["id"],
                integrity_block(
                    confirmation_block,
                    superseded=latest_observation != accepted_row["event_seq"],
                    query_pending=latest_query["id"] != accepted.query_id,
                    authorization=accepted.scope_block,
                ),
            ),
        )
        evidence = {
            "policy_head": dict(head),
            "policy_revision": dict(policy_row),
            "accepted_snapshot": dict(accepted_row),
            "accepted_query": dict(query),
            "latest_query": dict(latest_query),
            "prefixes": [dict(row) for row in sorted(prefixes, key=lambda row: row["event_seq"])],
            "events": [dict(row) for _, row in sorted(all_rows.items())],
            "decisions": [dict(decisions[key]) for key in sorted(decisions)],
            "resolution_observation_seqs": [
                [resolution_seq, observation_seq]
                for resolution_seq, observation_seq in sorted(
                    latest_reconcile_by_resolution.items()
                )
            ],
        }
        evidence_json = canonical_bytes(evidence)
        require(
            len(evidence_json) <= self.limits.max_payload_bytes,
            "history_payload_limit",
            bytes=len(evidence_json),
            limit=self.limits.max_payload_bytes,
        )
        context = getcontext()
        decimal_context = tuple(
            sorted(
                {
                    "prec": str(context.prec),
                    "rounding": context.rounding,
                    "Emin": str(context.Emin),
                    "Emax": str(context.Emax),
                    "capitals": str(context.capitals),
                    "clamp": str(context.clamp),
                    "traps": ",".join(
                        sorted(key.__name__ for key, enabled in context.traps.items() if enabled)
                    ),
                }.items()
            )
        )
        manifest = InputManifest(
            self.source_revision,
            scope,
            now_ms,
            max_snapshot_age_ms,
            self.limits,
            decimal_context,
            canonical_bytes(
                {
                    "watermark": watermark,
                    "watermark_prefix_hash": prefix_by_seq[watermark],
                    "accepted_seq": accepted_row["event_seq"],
                    "command_fence": accepted.attempt_seq_high_water,
                    "accepted_query_id": accepted.query_id,
                    "covered_prefix_hash": accepted_row["covered_prefix_hash"],
                    "latest_query_id": latest_query["id"],
                    "latest_query_revision": latest_query["query_revision"],
                    "latest_observation_seq": latest_observation,
                    "policy_revision_id": policy.revision_id,
                    "policy_revision": policy.revision,
                    "policy_digest": policy.digest,
                }
            ),
            evidence_json,
            canonical_bytes(
                {
                    "uncertainty_from_exclusive": 0,
                    "history_to_inclusive": watermark,
                    "commitment_from_exclusive": accepted.attempt_seq_high_water,
                    "uncertainty_rows": len(uncertainty_rows),
                    "tail_rows": len(tail_rows),
                    "point_lookups": reference_lookups,
                    "seq_only_rows": len(seq_only_rows),
                    "seq_only_row_bound": len(resolution_rows) + 1,
                    "payload_bytes": payload_bytes,
                    "evidence_bytes": len(evidence_json),
                    "uncertainty_row_limit": self.limits.max_history_rows,
                    "tail_row_limit": self.limits.max_history_rows,
                    "payload_byte_limit": self.limits.max_payload_bytes,
                    "point_lookup_limit": self.limits.max_reference_lookups,
                    "complete": True,
                }
            ),
            tuple((key, candidate_input_digest(value)) for key, value in sorted(evidence.items())),
            candidate_input_digest(inputs),
        )
        return LoadedInputs(inputs, manifest, candidate_input_digest(manifest.digest_payload()))
