"""Operator requests: the web API queues them, the account daemon applies them.

ADR D4': the web API holds no write on execution state. Every human action that
changes it -- resuming or killing trading, adjudicating
an uncertainty -- is a row in an outbox table. The web API inserts only the
request columns (a column-scoped grant) and answers 202; the account daemon's
worker applies the request inside the account's single writer, under the
account lock, re-checks the operator in that same transaction, and records
exactly one outcome on the row:

- ``applied``  -- done; what it produced names the request (``operator_request_id`` on the
  trading state, policy revision, cancel-all audit or journal row);
- ``rejected`` -- a bounded, operator-readable code (:class:`RequestRejected`);
- ``failed``   -- a fault, named by its root exception type.

Every outbox table follows the same contract, enforced in its migration: no
runtime role may UPDATE a request column (column grants), ``state`` leaves
``requested`` exactly once (a trigger, the owner included), a partial unique
index allows one pending request per subject (``pending_index``), rows are never
deleted. The model declares its column split as ``REQUEST_COLUMNS`` and
``WORKER_COLUMNS`` (tests pin each migration's grants to them).

The worker never holds a request hostage: a request whose outcome cannot be
written is marked failed in its own transaction, and one that cannot even be
marked is skipped by this process (a restart retries it), so one bad row cannot
block the queue behind it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, get_args

from sqlalchemy import Index
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

RequestState = Literal["requested", "applied", "rejected", "failed"]
REQUEST_STATES: tuple[str, ...] = get_args(RequestState)
REQUESTED: RequestState = "requested"
APPLIED: RequestState = "applied"
REJECTED: RequestState = "rejected"
FAILED: RequestState = "failed"

RejectionKind = Literal["conflict", "invalid", "not_found"]
OPERATOR_NOT_AUTHORIZED = "operator_not_authorized"


class RequestRejected(RuntimeError):  # noqa: N818 - domain outcome, not a fault
    """A bounded, operator-readable reason the request cannot apply."""

    def __init__(self, code: str, *, kind: RejectionKind = "conflict") -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind

async def insert_request(session: AsyncSession, model: Any, values: Mapping[str, object]) -> bool:
    """Queue one request from the web API; False when its pending slot is taken.

    An explicit column INSERT: the web API's grant covers exactly
    ``model.REQUEST_COLUMNS``, and an ORM flush would also send the worker's
    columns. ``ON CONFLICT DO NOTHING`` on the request's pending index
    (``model.pending_index``) is the only refusal answered with False; any other
    (a CHECK, a foreign key, the realm guard, a duplicate id) raises. Takes no
    account lock -- that lock serialises the daemon's writer, and the web API
    must never stall reconcile or submit.
    """
    if set(values) != set(model.REQUEST_COLUMNS):
        raise ValueError(f"request values must be exactly {model.REQUEST_COLUMNS}")
    name = model.pending_index(values)
    index = next(index for index in model.__table__.indexes if index.name == name)
    bind = session.bind
    dialect = bind.dialect.name if bind is not None else "postgresql"
    statement = (sqlite.insert(model) if dialect == "sqlite" else postgresql.insert(model)).values(
        **values).on_conflict_do_nothing(
        index_elements=list(index.columns), index_where=_predicate(index, dialect),
    ).returning(model.request_id)
    return await session.scalar(statement) is not None


def _predicate(index: Index, dialect: str) -> Any:
    where = index.dialect_options[dialect]["where"]
    if where is None:
        raise ValueError(f"{index.name} is not a partial index on {dialect}")
    return where



__all__ = [
    "APPLIED",
    "FAILED",
    "OPERATOR_NOT_AUTHORIZED",
    "REJECTED",
    "REQUESTED",
    "REQUEST_STATES",
    "RejectionKind",
    "RequestRejected",
    "RequestState",
    "insert_request",
]
