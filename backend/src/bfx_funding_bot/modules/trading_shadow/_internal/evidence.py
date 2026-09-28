"""Pure decoding of explicit SQL columns, without execution's ORM or store."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy, OfferEnvelope
from bfx_funding_bot.modules.trading import (
    AcceptedCapitalBasis,
    AppliedPolicy,
    Blocked,
    CapitalScope,
    SymbolCapital,
)

type Row = Mapping[str, Any]


class EvidenceError(ValueError):
    def __init__(self, reason: str, **evidence: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = tuple(sorted((key, str(value)) for key, value in evidence.items()))


def require(condition: bool, reason: str, **evidence: object) -> None:
    if not condition:
        raise EvidenceError(reason, **evidence)


def amount(value: object) -> Decimal:
    # capital_repository.py:176-185: never accept float/bool as capital amounts.
    try:
        require(isinstance(value, (str, Decimal)), "invalid_capital_amount")
        result = Decimal(str(value))
        require(result.is_finite() and result >= 0, "invalid_capital_amount")
        return result
    except InvalidOperation as exc:
        raise EvidenceError("invalid_capital_amount") from exc


def legacy_digest(value: object) -> str:
    # capital_repository.py:172-173: do NOT normalize Decimal/string spelling.
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def decode_policy(scope: CapitalScope, head: Row, row: Row) -> AppliedPolicy:
    require(
        (
            row["exchange_account_id"],
            row["deployment_environment"],
            row["symbol"],
            row["revision"],
            row["id"],
        )
        == (
            scope.account_id,
            scope.environment,
            scope.symbol,
            head["revision"],
            head["revision_id"],
        ),
        "inconsistent_policy_pointer",
    )
    raw = row["policy"]
    keys = {"enabled", "reserve_amount", "allocation_mode", "max_cell_fraction"}
    version = row["schema_version"]
    require(
        version in {1, 2, 3} and row["digest"] == legacy_digest(raw),
        "invalid_policy_schema_or_digest",
    )
    if version >= 2:
        keys.add("max_offer_amount")
    if version == 3:
        keys.add("envelope")
    require(set(raw) == keys, "invalid_policy")
    envelope = None
    if version == 3:
        e = raw["envelope"]
        require(
            set(e)
            == {
                "min_period_days",
                "max_period_days",
                "max_open_offers",
                "rate_floor_ratio",
                "min_rate_apr",
            },
            "invalid_policy",
        )
        envelope = OfferEnvelope(
            e["min_period_days"],
            e["max_period_days"],
            e["max_open_offers"],
            amount(e["rate_floor_ratio"]),
            amount(e["min_rate_apr"]),
        )
    policy = CapitalPolicy(
        enabled=raw["enabled"],
        reserve_amount=amount(raw["reserve_amount"]),
        allocation_mode=raw["allocation_mode"],
        max_cell_fraction=amount(raw["max_cell_fraction"]),
        max_offer_amount=amount(raw["max_offer_amount"]) if version >= 2 else None,
        envelope=envelope,
    )
    return AppliedPolicy(
        scope.account_id,
        scope.environment,
        scope.symbol,
        row["revision"],
        row["digest"],
        row["id"],
        policy,
    )


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    event_id: UUID
    kind: str
    payload: Row
    occurred_at_ms: int


def decode_event(row: Row, scope: CapitalScope, watermark: int) -> Event:
    require(
        (row["exchange_account_id"], row["deployment_environment"])
        == (scope.account_id, scope.environment)
        and 0 < row["event_seq"] <= watermark,
        "attempt_intent_scope_conflict",
        seq=row["event_seq"],
    )
    raw = row["payload"]
    version = raw.get("__schema_version__")
    require(
        row["schema_version"] in {2, 3}
        and row["schema_version"] == (2 if version is None else version),
        "snapshot_evidence_invalid",
        seq=row["event_seq"],
    )
    require(
        version is None or raw.get("__event_type__") == row["event_type"],
        "snapshot_evidence_invalid",
        seq=row["event_seq"],
    )
    if version == 3:
        identity = UUID(raw["event_id"])
        require(
            row["event_id"] is None or row["event_id"] == identity,
            "snapshot_evidence_invalid",
            seq=row["event_seq"],
        )
    else:
        # serialization.py:185-225 / projector.py:39-81: legacy identity uses
        # the OLD textual account, before canonical account upcasting.
        payload_json = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
        )
        name = json.dumps(
            [
                row["event_seq"],
                row["account_id"],
                scope.environment,
                row["event_type"],
                row["occurred_at_ms"],
                payload_json,
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        )
        identity = uuid5(uuid5(NAMESPACE_URL, "bfx-funding-bot/event-log/v2"), name)
    payload = dict(raw)
    payload.setdefault("__schema_version__", 2)
    payload.setdefault("__event_type__", row["event_type"])
    # serialization.py:243-254,281-286: durable UUID owns historical events;
    # symbol-less historical rows are fUST, never a fabricated decision/cycle.
    payload["account_id"] = str(scope.account_id)
    if version != 3 and payload.get("symbol") is None:
        payload["symbol"] = "fUST"
    require(
        payload.get("environment", scope.environment) == scope.environment,
        "attempt_intent_scope_conflict",
        seq=row["event_seq"],
    )
    if "cid" in payload:
        require(row["cid"] == payload["cid"], "attempt_intent_scope_conflict", seq=row["event_seq"])
    return Event(row["event_seq"], identity, row["event_type"], payload, row["occurred_at_ms"])


def _observation(payload: Row) -> dict[str, object]:
    result: dict[str, object] = {"wallet_available": payload["wallet_available"]}
    for key, identity in (("offers", "venue_offer_id"), ("credits", "credit_id")):
        objects: dict[str, object] = {}
        for item in payload[key]:
            item_id = item[identity]
            require(bool(item_id) and bool(item["symbol"]), "snapshot_evidence_invalid")
            if key == "offers":
                require(
                    amount(item["amount_remaining"]) <= amount(item["amount_original"]),
                    "snapshot_evidence_invalid",
                )
            else:
                amount(item["amount"])
            require(
                item_id not in objects or objects[item_id] == item, "snapshot_conflicting_identity"
            )
            require(item["symbol"] in payload["wallet_available"], "snapshot_missing_wallet")
            objects[item_id] = item
        result[key] = objects
    for value in payload["wallet_available"].values():
        amount(value)
    return result


def _snapshot_shape(payload: Row, reason: str) -> None:
    # serialization.py:121-142 and events.py:254-255: equality of wallet/offer
    # observations alone does not prove a valid serialized confirmation event.
    require(
        payload.get("__event_type__") == "VENUE_SNAPSHOT_OBSERVED"
        and payload.get("__schema_version__") in {2, 3},
        reason,
    )
    if payload["__schema_version__"] == 3:
        try:
            UUID(str(payload.get("event_id")))
        except ValueError as exc:
            raise EvidenceError(reason) from exc
    require(
        bool(payload.get("account_id"))
        and bool(payload.get("environment"))
        and type(payload.get("query_started_at_ms")) is int
        and type(payload.get("query_finished_at_ms")) is int
        and payload["query_started_at_ms"] <= payload["query_finished_at_ms"]
        and isinstance(payload.get("offers"), list)
        and isinstance(payload.get("credits"), list)
        and isinstance(payload.get("wallet_available"), Mapping),
        reason,
    )
    coverage = payload.get("coverage")
    require(isinstance(coverage, Mapping), reason)
    assert isinstance(coverage, Mapping)
    for name, count in (
        ("active_offers_complete", "active_offer_pages"),
        ("active_credits_complete", "active_credit_pages"),
        ("wallets_complete", "wallet_pages"),
        ("offer_history_complete", "offer_history_pages"),
    ):
        pages = coverage.get(count, 0 if name == "offer_history_complete" else 1)
        require(
            type(pages) is int and pages >= 0 and (not coverage.get(name) or pages >= 1), reason
        )
    require(
        all(
            name in coverage
            for name in (
                "active_offers_complete",
                "active_credits_complete",
                "wallets_complete",
            )
        ),
        reason,
    )
    start, end = coverage.get("offer_history_start_ms"), coverage.get("offer_history_end_ms")
    if coverage.get("offer_history_complete"):
        require(type(start) is int and type(end) is int, reason)
    if start is not None and end is not None:
        require(type(start) is int and type(end) is int and start <= end, reason)


def full_coverage(payload: Row) -> bool:
    return all(
        payload["coverage"].get(key) is True
        for key in (
            "active_offers_complete",
            "active_credits_complete",
            "wallets_complete",
        )
    )


def decode_basis(
    scope: CapitalScope, row: Row, query: Row, event: Event, prefix: str | None, watermark: int
) -> tuple[AcceptedCapitalBasis, Blocked | None]:
    require(row["schema_version"] == 1, "snapshot_unavailable")
    require(
        prefix is not None and prefix == row["covered_prefix_hash"],
        "snapshot_prefix_diverged",
        seq=row["event_seq"],
    )
    require(event.kind == "VENUE_SNAPSHOT_OBSERVED", "snapshot_evidence_invalid")
    p = event.payload
    _snapshot_shape(p, "snapshot_evidence_invalid")
    c = row["classification"]
    require(
        (
            query["exchange_account_id"],
            query["deployment_environment"],
            query["command_fence"],
            query["id"],
            query["started_at_ms"],
            p["capital_query_id"],
            p["capital_command_fence"],
            p["capital_classification_digest"],
        )
        == (
            scope.account_id,
            scope.environment,
            row["command_fence"],
            row["query_id"],
            p["query_started_at_ms"],
            str(row["query_id"]),
            row["command_fence"],
            legacy_digest(c),
        )
        and 0 <= row["command_fence"] < row["event_seq"] <= watermark,
        "snapshot_evidence_conflict",
    )
    # The basis itself is shared acceptance, NOT a second classifier.
    symbols = tuple(
        SymbolCapital(
            symbol,
            amount(values["available"]),
            amount(values["offered"]),
            amount(values["credits"]),
            amount(values["unattributed_credits"]),
            amount(values["foreign"]),
            tuple(sorted((cell, amount(value)) for cell, value in values["cells"].items())),
        )
        for symbol, values in sorted(c["symbols"].items())
    )
    reflected = tuple(UUID(key) for key in c["reflected"])
    settled = tuple(UUID(key) for key in c.get("settled", []))
    unresolved = tuple(
        sorted(
            ((UUID(key), symbol) for key, symbol in c.get("unresolved", {}).items()),
            key=lambda item: str(item[0]),
        )
    )
    ids = (*reflected, *settled, *(key for key, _ in unresolved))
    require(len(ids) == len(set(ids)), "snapshot_evidence_conflict")
    authorization = row["authorization_blocked_reason"]
    accepted = AcceptedCapitalBasis(
        scope.account_id,
        scope.environment,
        row["event_seq"],
        row["command_fence"],
        row["query_id"],
        p["query_started_at_ms"],
        p["query_finished_at_ms"],
        symbols,
        frozenset(reflected),
        frozenset(settled),
        unresolved,
        "credit_cells" in c,
        Blocked(authorization, ()) if authorization is not None else None,
    )
    failure = None
    confirmation = p.get("capital_confirmation")
    if confirmation is None:
        failure = Blocked("snapshot_confirmation_missing", ())
    else:
        _snapshot_shape(confirmation, "snapshot_confirmation_conflict")
        if _observation(confirmation) != _observation(p):
            failure = Blocked("snapshot_confirmation_conflict", ())
        elif not full_coverage(p):
            failure = Blocked("snapshot_incomplete", ())
    return accepted, failure
