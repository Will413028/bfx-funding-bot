"""Two-read observation acceptance; an accepted one writes its capital basis."""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    CREDIT_STATUSES,
    CREDIT_TERMINAL_KINDS,
    OFFER_STATUSES,
    OFFER_TERMINAL_KINDS,
    Acceptance,
    CreditHistory,
    JsonObject,
    Observation,
    OfferHistory,
    Quarantine,
    QuarantineMember,
    QueryHandle,
    Scope,
    Wallet,
)
from bfx_funding_bot.modules.ledger._internal import history_symbols, unconfirmed_ends
from bfx_funding_bot.modules.ledger._internal.basis import previous_basis, write_basis
from bfx_funding_bot.modules.ledger._internal.clock import lock_scope
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload
from bfx_funding_bot.modules.ledger._internal.quarantine import (
    add_quarantine_member,
    open_quarantine,
    unresolved_quarantines,
)
from bfx_funding_bot.modules.ledger.tables import (
    CapitalCommandClockRow,
    LedgerObservationCreditHistoryRow,
    LedgerObservationCreditRow,
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    LedgerObservationTradeRow,
    LedgerObservationWalletRow,
    QuarantineMemberRow,
    VenueCreditMirrorRow,
    VenueOfferMirrorRow,
)


def _unique[K: Hashable, T](values: tuple[T, ...], key: Callable[[T], K]) -> dict[K, T]:
    result: dict[K, T] = {}
    for value in values:
        identity = key(value)
        if identity in result and result[identity] != value:
            raise ValueError(f"conflicting observation identity: {identity!r}")
        result[identity] = value
    return result


def _encoded(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, dict):
        return {key: _encoded(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encoded(item) for item in value]
    return value


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite observation amount")
    return "0" if value == 0 else format(value.normalize(), "f")


def _check_json(value: object) -> None:
    """Raw venue evidence must already be JSON: the adapter owns its encoding.

    A Decimal is refused, not converted: the port type says ``JsonObject``, and the only
    converter lives where the wire format is known (``venue_observation``).
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number in raw venue evidence")
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("raw venue evidence has a non-string key")
        for item in value.values():
            _check_json(item)
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            _check_json(item)
        return
    raise ValueError(f"raw venue evidence is not JSON-representable: {type(value).__name__}")


def _nonnegative(value: Decimal | None) -> bool:
    return value is None or (value.is_finite() and value >= 0)


def digest_bytes(observation: Observation) -> bytes:
    """Version 1: sorted identity maps, decimal strings, UTF-8 canonical JSON."""
    wallets: dict[tuple[str, str], Wallet] = _unique(
        observation.wallets, lambda x: (x.wallet_type, x.currency)
    )
    offers = _unique(observation.offers, lambda x: x.venue_offer_id)
    credits = _unique(observation.credits, lambda x: (x.source_kind, x.venue_credit_id))
    payload: JsonObject = {
        "domain": "bfx-ledger-observation-active",
        "version": 1,
        "wallet_available": [
            [*key, _decimal(value.available)] for key, value in sorted(wallets.items())
        ],
        "offers": [_encoded(asdict(offers[key])) for key in sorted(offers)],
        "credits_and_loans": [_encoded(asdict(credits[key])) for key in sorted(credits)],
    }
    return canonical_payload(payload)


def digest(observation: Observation) -> str:
    return sha256(digest_bytes(observation)).hexdigest()


def _validate(observation: Observation) -> None:
    coverage = observation.coverage
    if observation.finished_at_ms < 0:
        raise ValueError("negative observation finish time")
    if any(
        value < 0
        for value in (
            coverage.wallet_pages,
            coverage.offer_pages,
            coverage.credit_pages,
            coverage.loan_pages,
            coverage.offer_history_pages,
            coverage.credit_history_pages,
        )
    ):
        raise ValueError("negative page count")
    for start, end in (
        (coverage.history_requested_start_ms, coverage.history_requested_end_ms),
        (coverage.history_oldest_mts_created, coverage.history_newest_mts_created),
        (coverage.trades_requested_start_ms, coverage.trades_requested_end_ms),
    ):
        if (start is None) != (end is None) or (
            start is not None and (start < 0 or end is None or end < start)
        ):
            raise ValueError("invalid history range")
    _unique(observation.wallets, lambda x: (x.wallet_type, x.currency))
    _unique(observation.offers, lambda x: x.venue_offer_id)
    _unique(observation.credits, lambda x: (x.source_kind, x.venue_credit_id))
    for wallet in observation.wallets:
        if (
            not wallet.wallet_type
            or not wallet.currency
            or wallet.symbol == ""
            or not all(_nonnegative(value) for value in (wallet.available, wallet.balance))
        ):
            raise ValueError("negative wallet amount")
        if wallet.wallet_type == "funding" and wallet.symbol is None:
            raise ValueError("funding wallet without symbol")
    symbols = [wallet.symbol for wallet in observation.wallets if wallet.symbol is not None]
    if len(symbols) != len(set(symbols)):
        raise ValueError("wallet symbol assigned twice")
    for offer in (*observation.offers, *(item.offer for item in observation.offer_history)):
        if (
            not offer.venue_offer_id
            or not offer.symbol
            or not all(
                _nonnegative(value)
                for value in (offer.amount_remaining, offer.amount_original, offer.rate)
            )
            or (offer.period_days is not None and offer.period_days <= 0)
            or offer.status not in OFFER_STATUSES
            or offer.mts_created < 0
            or (offer.mts_updated is not None and offer.mts_updated < 0)
        ):
            raise ValueError("invalid offer")
        _check_json(offer.raw)
    for credit in (*observation.credits, *(item.credit for item in observation.credit_history)):
        if (
            credit.source_kind not in ("credit", "loan")
            or not credit.venue_credit_id
            or credit.venue_credit_id.startswith("loan:")
            or credit.status not in CREDIT_STATUSES
            or credit.mts_opening is None
            or not credit.symbol
            or not all(_nonnegative(value) for value in (credit.amount, credit.rate))
            or (credit.period_days is not None and credit.period_days <= 0)
            or any(
                t is not None and t < 0
                for t in (credit.mts_created, credit.mts_updated, credit.mts_opening)
            )
        ):
            raise ValueError("invalid credit or loan")
        _check_json(credit.raw)
    if any(
        item.occurred_at_ms < 0 or item.terminal_kind not in OFFER_TERMINAL_KINDS
        for item in observation.offer_history
    ) or any(
        item.occurred_at_ms < 0 or item.terminal_kind not in CREDIT_TERMINAL_KINDS
        for item in observation.credit_history
    ):
        raise ValueError("invalid terminal history")
    if any(not offer_id for offer_id in observation.unconfirmed_ends) or len(
        set(observation.unconfirmed_ends)
    ) != len(observation.unconfirmed_ends):
        raise ValueError("invalid unconfirmed offer ends")
    _unique(observation.trades, lambda x: x.trade_id)
    for trade in observation.trades:
        if (
            trade.trade_id < 0
            or not trade.symbol
            or not trade.venue_offer_id
            or not (trade.amount.is_finite() and trade.amount > 0)
            or not _nonnegative(trade.rate)
            or trade.period_days <= 0
            or trade.mts_create < 0
        ):
            raise ValueError("invalid funding trade")
    digest_bytes(observation)


async def accept_observation(
    session: AsyncSession,
    scope: Scope,
    handle: QueryHandle,
    first: Observation,
    confirmation: Observation,
    confirmation_started_at_ms: int,
) -> Acceptance:
    """Store matched full observations; an accepted one also gets its capital basis."""
    _validate(first)
    _validate(confirmation)
    if confirmation.offer_history or confirmation.credit_history or confirmation.trades:
        raise ValueError("confirmation must contain active state only")
    if not (
        handle.started_at_ms
        <= first.finished_at_ms
        <= confirmation_started_at_ms
        <= confirmation.finished_at_ms
    ):
        raise ValueError("observation times overlap the committed query")
    first_digest, confirmation_digest = digest(first), digest(confirmation)
    if (
        not first.coverage.complete
        or not confirmation.coverage.active_complete
        or first_digest != confirmation_digest
    ):
        return Acceptance("incomplete_or_unequal", None, first_digest, confirmation_digest)
    await lock_scope(session, scope)
    query = await session.get(LedgerObservationQueryRow, handle.query_id)
    if query is None or (
        query.exchange_account_id,
        query.deployment_environment,
        query.query_revision,
        query.start_revision,
        query.started_at_ms,
    ) != (
        scope.exchange_account_id,
        scope.deployment_environment,
        handle.query_revision,
        handle.start_revision,
        handle.started_at_ms,
    ):
        raise ValueError("query handle is missing, uncommitted, or out of scope")
    latest = await session.scalar(
        select(func.max(LedgerObservationQueryRow.query_revision)).where(
            LedgerObservationQueryRow.exchange_account_id == scope.exchange_account_id,
            LedgerObservationQueryRow.deployment_environment == scope.deployment_environment,
        )
    )
    clock = await session.scalar(
        select(CapitalCommandClockRow.revision).where(
            CapitalCommandClockRow.exchange_account_id == scope.exchange_account_id,
            CapitalCommandClockRow.deployment_environment == scope.deployment_environment,
        )
    )
    accepted = latest == handle.query_revision and (clock or 0) == handle.start_revision
    if accepted and clock is None:
        await session.execute(
            insert(CapitalCommandClockRow).values(
                exchange_account_id=scope.exchange_account_id,
                deployment_environment=scope.deployment_environment,
                revision=0,
            )
        )
    observation_id = uuid4()
    coverage = first.coverage
    session.add(
        LedgerObservationRow(
            id=observation_id,
            query_id=handle.query_id,
            exchange_account_id=scope.exchange_account_id,
            deployment_environment=scope.deployment_environment,
            schema_version=1,
            query_finished_at_ms=first.finished_at_ms,
            confirmation_finished_at_ms=confirmation.finished_at_ms,
            accept_revision=handle.start_revision,
            wallets_complete=coverage.wallets_complete,
            offers_complete=coverage.offers_complete,
            credits_complete=coverage.credits_complete,
            loans_complete=coverage.loans_complete,
            offer_history_complete=coverage.offer_history_complete,
            credit_history_complete=coverage.credit_history_complete,
            trades_complete=coverage.trades_complete,
            trades_requested_start_ms=coverage.trades_requested_start_ms,
            trades_requested_end_ms=coverage.trades_requested_end_ms,
            offer_history_pages=coverage.offer_history_pages,
            credit_history_pages=coverage.credit_history_pages,
            history_requested_start_ms=coverage.history_requested_start_ms,
            history_requested_end_ms=coverage.history_requested_end_ms,
            history_oldest_mts_created=coverage.history_oldest_mts_created,
            history_newest_mts_created=coverage.history_newest_mts_created,
            first_digest=first_digest,
            confirmation_digest=confirmation_digest,
            accepted=accepted,
            evidence={
                history_symbols.KEY: history_symbols.encode(coverage.history_symbols),
                "confirmation_started_at_ms": confirmation_started_at_ms,
                # Read back by the basis (conservation), like history_symbols above.
                unconfirmed_ends.KEY: unconfirmed_ends.encode(first.unconfirmed_ends),
                "first_page_counts": {
                    "wallet": coverage.wallet_pages,
                    "offer": coverage.offer_pages,
                    "credit": coverage.credit_pages,
                    "loan": coverage.loan_pages,
                },
                "confirmation_page_counts": {
                    "wallet": confirmation.coverage.wallet_pages,
                    "offer": confirmation.coverage.offer_pages,
                    "credit": confirmation.coverage.credit_pages,
                    "loan": confirmation.coverage.loan_pages,
                },
            },
        )
    )
    await session.flush()
    for wallet in _unique(first.wallets, lambda x: (x.wallet_type, x.currency)).values():
        session.add(LedgerObservationWalletRow(observation_id=observation_id, **asdict(wallet)))
    for offer in _unique(first.offers, lambda x: x.venue_offer_id).values():
        session.add(
            LedgerObservationOfferRow(id=uuid4(), observation_id=observation_id, **asdict(offer))
        )
    for credit in _unique(first.credits, lambda x: (x.source_kind, x.venue_credit_id)).values():
        session.add(
            LedgerObservationCreditRow(id=uuid4(), observation_id=observation_id, **asdict(credit))
        )
    for trade in _unique(first.trades, lambda x: x.trade_id).values():
        session.add(LedgerObservationTradeRow(observation_id=observation_id, **asdict(trade)))
    offer_history: dict[str, tuple[UUID, OfferHistory]] = {}
    credit_history: dict[tuple[str, str], tuple[UUID, CreditHistory]] = {}
    for item in first.offer_history:
        row_id = uuid4()
        session.add(
            LedgerObservationOfferHistoryRow(
                id=row_id,
                observation_id=observation_id,
                terminal_kind=item.terminal_kind,
                occurred_at_ms=item.occurred_at_ms,
                **asdict(item.offer),
            )
        )
        previous = offer_history.get(item.offer.venue_offer_id)
        if previous is None or item.occurred_at_ms > previous[1].occurred_at_ms:
            offer_history[item.offer.venue_offer_id] = (row_id, item)
    for credit_item in first.credit_history:
        row_id = uuid4()
        session.add(
            LedgerObservationCreditHistoryRow(
                id=row_id,
                observation_id=observation_id,
                terminal_kind=credit_item.terminal_kind,
                occurred_at_ms=credit_item.occurred_at_ms,
                **asdict(credit_item.credit),
            )
        )
        credit_key = (credit_item.credit.source_kind, credit_item.credit.venue_credit_id)
        credit_previous = credit_history.get(credit_key)
        if (
            credit_previous is None
            or credit_item.occurred_at_ms > credit_previous[1].occurred_at_ms
        ):
            credit_history[credit_key] = (row_id, credit_item)
    await session.flush()
    if accepted:
        await _mirror_offers(session, scope, observation_id, first, offer_history)
        await _mirror_credits(session, scope, observation_id, first, credit_history)
        await write_basis(session, scope, observation_id)
    return Acceptance(
        "accepted" if accepted else "fenced", observation_id, first_digest, confirmation_digest
    )


async def _mirror_offers(
    session: AsyncSession,
    scope: Scope,
    observation_id: UUID,
    first: Observation,
    history: dict[str, tuple[UUID, OfferHistory]],
) -> None:
    observed = _unique(first.offers, lambda x: x.venue_offer_id)
    rows = (
        await session.scalars(
            select(VenueOfferMirrorRow).where(
                VenueOfferMirrorRow.exchange_account_id == scope.exchange_account_id,
                VenueOfferMirrorRow.deployment_environment == scope.deployment_environment,
                or_(
                    VenueOfferMirrorRow.present_in_latest_accepted_snapshot,
                    VenueOfferMirrorRow.venue_offer_id.in_((*observed, *history)),
                ),
            )
        )
    ).all()
    existing = {row.venue_offer_id: row for row in rows}
    for offer_id, row in existing.items():
        offer = observed.get(offer_id)
        if offer is None:
            row.present_in_latest_accepted_snapshot = False
            row.last_accepted_observation_id = observation_id
            terminal = history.get(offer_id)
            if (
                terminal is not None
                and row.terminal_evidence_id is None
                and row.symbol == terminal[1].offer.symbol
            ):
                row.terminal_evidence_id, row.terminal_kind = terminal[0], terminal[1].terminal_kind
        elif row.terminal_evidence_id is not None:
            await _quarantine_reappearance(
                session,
                scope,
                observation_id,
                "offer",
                offer_id,
                offer.symbol,
                offer.amount_remaining,
                first.finished_at_ms,
            )
            row.present_in_latest_accepted_snapshot = False
            row.last_accepted_observation_id = observation_id
        else:
            for key, value in asdict(offer).items():
                if key != "raw":
                    setattr(row, key, value)
            row.present_in_latest_accepted_snapshot = True
            row.last_accepted_observation_id = observation_id
    for offer_id, offer in observed.items():
        if offer_id not in existing:
            data = asdict(offer)
            data.pop("raw")
            session.add(
                VenueOfferMirrorRow(
                    exchange_account_id=scope.exchange_account_id,
                    deployment_environment=scope.deployment_environment,
                    last_accepted_observation_id=observation_id,
                    present_in_latest_accepted_snapshot=True,
                    terminal_evidence_id=None,
                    terminal_kind=None,
                    **data,
                )
            )
    await session.flush()


async def _mirror_credits(
    session: AsyncSession,
    scope: Scope,
    observation_id: UUID,
    first: Observation,
    history: dict[tuple[str, str], tuple[UUID, CreditHistory]],
) -> None:
    observed = _unique(first.credits, lambda x: (str(x.source_kind), x.venue_credit_id))
    ids = [key[1] for key in (*observed, *history)]
    rows = (
        await session.scalars(
            select(VenueCreditMirrorRow).where(
                VenueCreditMirrorRow.exchange_account_id == scope.exchange_account_id,
                VenueCreditMirrorRow.deployment_environment == scope.deployment_environment,
                or_(
                    VenueCreditMirrorRow.present_in_latest_accepted_snapshot,
                    VenueCreditMirrorRow.venue_credit_id.in_(ids),
                ),
            )
        )
    ).all()
    existing = {(row.source_kind, row.venue_credit_id): row for row in rows}
    for credit_key, row in existing.items():
        credit = observed.get(credit_key)
        if credit is None:
            row.present_in_latest_accepted_snapshot = False
            row.last_accepted_observation_id = observation_id
            terminal = history.get(credit_key)
            if (
                terminal is not None
                and row.terminal_evidence_id is None
                and row.symbol == terminal[1].credit.symbol
            ):
                row.terminal_evidence_id, row.terminal_kind = terminal[0], terminal[1].terminal_kind
        elif row.terminal_evidence_id is not None:
            await _quarantine_reappearance(
                session,
                scope,
                observation_id,
                credit.source_kind,
                credit.venue_credit_id,
                credit.symbol,
                credit.amount,
                first.finished_at_ms,
            )
            row.present_in_latest_accepted_snapshot = False
            row.last_accepted_observation_id = observation_id
        else:
            for name, value in asdict(credit).items():
                if name != "raw":
                    setattr(row, name, value)
            row.present_in_latest_accepted_snapshot = True
            row.last_accepted_observation_id = observation_id
    for credit_key, credit in observed.items():
        if credit_key not in existing:
            data = asdict(credit)
            data.pop("raw")
            session.add(
                VenueCreditMirrorRow(
                    exchange_account_id=scope.exchange_account_id,
                    deployment_environment=scope.deployment_environment,
                    last_accepted_observation_id=observation_id,
                    present_in_latest_accepted_snapshot=True,
                    terminal_evidence_id=None,
                    terminal_kind=None,
                    **data,
                )
            )
    await session.flush()


async def _quarantine_reappearance(
    session: AsyncSession,
    scope: Scope,
    observation_id: UUID,
    kind: Literal["offer", "credit", "loan"],
    venue_id: str,
    symbol: str,
    amount: Decimal,
    opened_at_ms: int,
) -> None:
    """Quarantine an identity seen active after confirmed terminal evidence.

    Merge like legacy quarantines: an identity already in an unresolved
    plain quarantine is left alone, and a new identity joins the symbol's
    unresolved plain quarantine as a member (never an R6 source quarantine)
    instead of opening one per acceptance.
    """
    # The basis being accepted is not written yet: bound by the previous one.
    previous = await previous_basis(session, scope)
    open_ids = [
        row.quarantine_id
        for row in await unresolved_quarantines(session, scope, previous, symbol=symbol)
        if row.source_attempt_id is None
    ]
    if open_ids:
        already = await session.scalar(
            select(QuarantineMemberRow.quarantine_id)
            .where(
                QuarantineMemberRow.quarantine_id.in_(open_ids),
                QuarantineMemberRow.source_kind == kind,
                QuarantineMemberRow.venue_object_id == venue_id,
            )
            .limit(1)
        )
        if already is not None:
            return
        quarantine_id = open_ids[0]
    else:
        quarantine_id = uuid4()
        await open_quarantine(
            session,
            scope,
            Quarantine(
                quarantine_id,
                symbol,
                amount,
                opened_at_ms,
                {
                    "reason": "terminal_identity_reappeared",
                    "source_kind": kind,
                    "venue_object_id": venue_id,
                },
            ),
        )
    await add_quarantine_member(
        session,
        scope,
        QuarantineMember(
            quarantine_id,
            kind,
            venue_id,
            observation_id,
            amount,
        ),
    )
