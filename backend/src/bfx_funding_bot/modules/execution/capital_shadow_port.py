"""Read-only baseline observations; no shadow or persistence dependency."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from bfx_funding_bot.modules.trading import (
    CapitalBudget,
    CapitalPolicy,
    CapitalSnapshot,
)


@dataclass(frozen=True, slots=True)
class BaselineAvailable:
    account_id: UUID
    environment: str
    symbol: str
    revision: int
    digest: str
    revision_id: UUID
    policy: CapitalPolicy
    snapshot_seq: int
    command_fence: int
    query_id: UUID
    snapshot: CapitalSnapshot
    budget: CapitalBudget
    unattributed_credit_exposure: Decimal
    projection_cursor: int
    watermark: int
    status: Literal["available"] = "available"


@dataclass(frozen=True, slots=True)
class BaselineBlocked:
    reason: str
    projection_cursor: int | None = None
    watermark: int | None = None
    status: Literal["blocked"] = "blocked"


@dataclass(frozen=True, slots=True)
class BaselineNotComparable:
    reason: str
    projection_cursor: int | None
    watermark: int
    status: Literal["not_comparable"] = "not_comparable"


type BaselineResult = BaselineAvailable | BaselineBlocked | BaselineNotComparable


class CapitalScopeLike(Protocol):
    @property
    def account_id(self) -> UUID: ...

    @property
    def environment(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def cell_id(self) -> str: ...
