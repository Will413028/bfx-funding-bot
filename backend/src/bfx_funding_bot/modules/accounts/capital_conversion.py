"""Explicit operator conversion of legacy sources, never a runtime fallback.

Drafts are inert and preserved. Apply binds a reviewed report digest to the
draft revision/content, active policies and capital snapshot. No halt write,
venue client, environment-variable capital loader or automatic resume exists.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.tables import AccountConfigDraft
from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
from bfx_funding_bot.modules.execution.capital_repository import (
    CapitalBlockedError,
    CapitalRepository,
    policy_payload,
)
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyHeadRow,
    CapitalPolicyRevisionRow,
)
from bfx_funding_bot.modules.execution.safety.hard_guards import resolve_for_symbol_with_source
from bfx_funding_bot.modules.strategy import canonical_cell_id

_KEYS = {"schema_version", "caps", "default_cap", "env_fallback_cap", "buffers",
         "default_buffer", "env_fallback_buffer", "max_cell_fraction"}


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _legacy_values(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    invalid = sorted(set(raw) ^ _KEYS)
    normalized: dict[str, Any] = {"schema_version": 1}
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        invalid.append("schema_version")

    def number(key: str, value: object, *, optional: bool = False) -> str | None:
        if optional and value is None:
            return None
        try:
            if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
                raise ValueError
            result = Decimal(value)
            if not result.is_finite() or result < 0:
                raise ValueError
            return str(result)
        except (InvalidOperation, ValueError, TypeError):
            invalid.append(key)
            return None

    for key in ("caps", "buffers"):
        value = raw.get(key)
        normalized[key] = {}
        if not isinstance(value, dict):
            invalid.append(key)
            continue
        for symbol, amount in value.items():
            if symbol not in {"fUST", "fUSD"}:
                invalid.append(f"{key}.{symbol}")
            normalized[key][symbol] = number(f"{key}.{symbol}", amount)
    for key in ("default_cap", "default_buffer", "env_fallback_cap", "env_fallback_buffer",
                "max_cell_fraction"):
        normalized[key] = number(key, raw.get(key), optional=key.startswith("env_"))
    fraction = normalized["max_cell_fraction"]
    if fraction is not None and not Decimal("0") < Decimal(fraction) <= Decimal("0.70"):
        invalid.append("max_cell_fraction")
    return normalized, sorted(set(invalid))


async def convert_capital_policy(
    session: AsyncSession, *, repository: CapitalRepository, legacy: dict[str, Any],
    now_ms: int, apply_digest: str | None,
) -> dict[str, Any]:
    """Return reviewable dry-run, or apply that exact report in the same txn.

    The caller commits only explicit apply. A stale digest fails instead of
    silently recalculating a different conversion. An exact repeated apply is
    idempotent; later policy or draft changes invalidate it.
    """
    await repository.writer.prepare_locked(session, account_id=repository.account_id)
    source, invalid = _legacy_values(legacy)
    if invalid:
        if apply_digest is not None:
            raise CapitalBlockedError("invalid_legacy_settings")
        return {"status": "invalid", "invalid_items": invalid}
    draft = await session.scalar(select(AccountConfigDraft).where(
        AccountConfigDraft.exchange_account_id == repository.account_id).with_for_update())
    draft_evidence = None if draft is None else {
        "id": str(draft.id), "revision": draft.revision, "config_digest": _digest(draft.config),
        "source": draft.source,
    }
    current: dict[str, Any] = {}
    for symbol in ("fUST", "fUSD"):
        head = await session.get(CapitalPolicyHeadRow,
            (repository.account_id, repository.environment, symbol), populate_existing=True)
        if head is None:
            current[symbol] = None
        else:
            # An unknown schema/digest must not become an implicit legacy fallback.
            await repository.read_applied(session, symbol=symbol)
            current[symbol] = await session.get(CapitalPolicyRevisionRow, head.revision_id)
    if apply_digest is not None and all(
        row is not None and row.source == {"conversion_digest": apply_digest,
            "draft": draft_evidence, "legacy_sources": source}
        for row in current.values()
    ):
        return {"status": "already_applied", "conversion_digest": apply_digest,
                "resumed": False}

    report: dict[str, Any] = {"status": "dry_run", "account_id": str(repository.account_id),
        "environment": repository.environment, "legacy_sources": source,
        "draft": draft_evidence, "symbols": {}, "disabled_symbols": ["fUSD"],
        "invalid_items": [], "resumed": False,
        "comparison_basis": "delta compares canonical new headroom with legacy account-cap/buffer limit; legacy in-memory per-cell tracker and active-cell relaxation cannot be reconstructed"}
    policies: dict[str, CapitalPolicy] = {}
    unavailable = False
    for symbol in ("fUST", "fUSD"):
        policy = CapitalPolicy(enabled=symbol == "fUST", reserve_amount=Decimal("0"),
                               max_cell_fraction=Decimal(source["max_cell_fraction"]))
        policies[symbol] = policy
        old = {}
        for label, mapping, fallback, default in (
            ("cap", "caps", "env_fallback_cap", "default_cap"),
            ("buffer", "buffers", "env_fallback_buffer", "default_buffer"),
        ):
            resolved = resolve_for_symbol_with_source(
                {key: Decimal(value) for key, value in source[mapping].items()}, symbol,
                env_fallback=Decimal(source[fallback]) if source[fallback] is not None else None,
                default=Decimal(source[default]))
            old[label], old[f"{label}_source"] = str(resolved.value), resolved.source
        previous = current[symbol]
        values: dict[str, Any] = {"old_effective": old, "new_policy": policy_payload(policy),
            "expected_revision": previous.revision if previous is not None else 0,
            "old_applied_policy": previous.policy if previous is not None else None, "cells": {}}
        for period in ("a30", "p2"):
            cell = canonical_cell_id(symbol, period)
            if not policy.enabled:
                values["cells"][cell] = {"status": "disabled", "capital_evaluated": False}
                continue
            try:
                view = await repository.preview_policy(session, symbol=symbol, cell_id=cell,
                                                       now_ms=now_ms, policy=policy)
            except CapitalBlockedError as exc:
                values["cells"][cell] = {"unavailable": str(exc)}
                unavailable = True
                continue
            snap = view.snapshot
            # Legacy account cap applied to total deployed/committed exposure;
            # this is a conversion comparison, never permission to submit.
            total_exposure = snap.total_capital - snap.available_amount + snap.unreflected_commitments
            old_cash = max(Decimal("0"), snap.available_amount - snap.unreflected_commitments
                           - Decimal(old["buffer"]))
            old_gap = max(Decimal("0"), Decimal(old["cap"]) - total_exposure)
            old_amount = min(old_cash, old_gap)
            values["cells"][cell] = {"snapshot_seq": view.snapshot_seq,
                "available": str(snap.available_amount), "pending": str(snap.unreflected_commitments),
                "total_capital": str(snap.total_capital), "cell_exposure": str(snap.cell_exposure),
                "unattributed_credit_exposure": str(view.unattributed_credit_exposure),
                "old_account_cap_limited_amount": str(old_amount),
                "new_cell_limit": str(view.budget.cell_limit),
                "new_cell_headroom": str(view.budget.cell_headroom),
                "new_max_new_offer": str(view.budget.max_new_offer),
                "delta": str(view.budget.max_new_offer - old_amount)}
        report["symbols"][symbol] = values
    digest = _digest(report)
    report["conversion_digest"] = digest
    if apply_digest is None:
        return report
    if apply_digest != digest:
        raise CapitalBlockedError("conversion_changed")
    if unavailable:
        raise CapitalBlockedError("conversion_snapshot_unavailable")
    for symbol, policy in policies.items():
        await repository.apply_policy(session, symbol=symbol, policy=policy,
            expected_revision=report["symbols"][symbol]["expected_revision"],
            source={"conversion_digest": digest, "draft": draft_evidence, "legacy_sources": source})
    report["status"] = "applied"
    return report
