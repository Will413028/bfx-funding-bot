"""Reverse policy-scope inventory, read in the comparison's one snapshot.

The comparison only looks at the scopes it was told about (``--scope`` accounts x
the cells file). This proves the database holds no authority state outside them:

* every (account, environment) in a table that can hold authority state is a listed
  scope (policy heads, snapshots, attempts, open uncertainties, open claims, trading
  state, pending requests, every scope-bearing ledger table);
* every (account, environment, symbol) policy head maps to a configured cell or to an
  explicit ``policy_without_cell`` declaration of the manifest;
* every symbol with live legacy offers or credits has a cell.

Any violation is ``not_comparable`` with evidence; the command exits non-zero. A
table the inventory cannot find is a violation too (fail closed, never a silent skip).
The cutover reader holds column SELECT on exactly the columns named here
(``alembic/versions/b5c6d7e8f9a0_*``).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.trading import CapitalScope

_OPEN_CLAIMS: Final = "state IN ('pending', 'unknown', 'claimed')"
_REQUESTED: Final = "state = 'requested'"
# (table, row filter): the filter keeps only rows that still carry authority state.
LEGACY_SCOPE_SOURCES: Final[tuple[tuple[str, str | None], ...]] = (
    ("capital_policy_heads", None),
    ("capital_snapshots", None),
    ("submission_attempts", None),
    ("execution_uncertainties", "state = 'open'"),
    ("offer_claims", _OPEN_CLAIMS),
    ("trading_state", None),
    ("uncertainty_resolution_requests", _REQUESTED),
    ("capital_policy_requests", _REQUESTED),
    ("trading_control_requests", _REQUESTED),
)
# Every ledger table that carries the scope columns itself (children join to these).
LEDGER_SCOPE_TABLES: Final = (
    "capital_command_clock",
    "ledger_observation_query",
    "ledger_observation",
    "venue_offer_mirror",
    "venue_credit_mirror",
    "submission_attempt_journal",
    "quarantine_opening",
    "execution_resolution_journal",
    "accepted_capital_basis",
)
_SOURCES: Final = (*LEGACY_SCOPE_SOURCES, *((table, None) for table in LEDGER_SCOPE_TABLES))


@dataclass(frozen=True)
class InventoryResult:
    status: str  # "ok" | "not_comparable"
    violations: tuple[dict[str, str], ...]


def _violation(reason: str, **evidence: object) -> dict[str, str]:
    return {"reason": reason, **{key: str(value) for key, value in evidence.items()}}


def live_symbols(classification: Any) -> set[str]:
    """Symbols with an active offer or credit in an accepted legacy classification."""
    if not isinstance(classification, dict):
        raise ValueError
    symbols: set[str] = set()
    for key in ("offers", "credit_cells"):
        for entry in (classification.get(key) or {}).values():
            symbols.add(entry["symbol"])
    for symbol, values in (classification.get("symbols") or {}).items():
        if Decimal(values["offered"]) > 0 or Decimal(values["credits"]) > 0:
            symbols.add(symbol)
    return symbols


async def _exists(session: AsyncSession, table: str) -> bool:
    return bool(await session.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}))


async def reverse_inventory(
    session: AsyncSession,
    *,
    scopes: Sequence[CapitalScope],
    policy_without_cell: frozenset[tuple[UUID, str, str]],
) -> InventoryResult:
    listed = {(scope.account_id, scope.environment) for scope in scopes}
    cells = {(scope.account_id, scope.environment, scope.symbol) for scope in scopes}
    violations: list[dict[str, str]] = []

    for table, where in _SOURCES:
        if not await _exists(session, table):
            violations.append(_violation("inventory_table_missing", table=table))
            continue
        clause = f" WHERE {where}" if where else ""
        rows = await session.execute(
            text(
                f"SELECT DISTINCT exchange_account_id, deployment_environment FROM public.{table}{clause}"
            )
        )
        violations.extend(
            _violation("scope_unlisted", table=table, account_id=account, environment=environment)
            for account, environment in rows.all()
            if (account, environment) not in listed
        )

    heads = await session.execute(
        text(
            "SELECT DISTINCT exchange_account_id, deployment_environment, symbol "
            "FROM public.capital_policy_heads"
        )
    )
    for account, environment, symbol in heads.all():
        key = (account, environment, symbol)
        if (account, environment) in listed and key not in cells and key not in policy_without_cell:
            violations.append(
                _violation(
                    "policy_symbol_without_cell",
                    table="capital_policy_heads",
                    account_id=account,
                    environment=environment,
                    symbol=symbol,
                )
            )

    # Live offers/credits come from the latest accepted snapshot and open claims only. The legacy
    # projections (venue_offer_state / venue_credit_state) are not readable by the cutover reader
    # (no column grant); 4e's anti-joins compare legacy live offers/credits at the capture point.
    live: set[tuple[UUID, str, str]] = set()
    latest = await session.execute(
        text(
            "SELECT DISTINCT ON (exchange_account_id, deployment_environment) "
            "exchange_account_id, deployment_environment, classification FROM public.capital_snapshots "
            "ORDER BY exchange_account_id, deployment_environment, event_seq DESC"
        )
    )
    for account, environment, classification in latest.all():
        try:
            live.update((account, environment, symbol) for symbol in live_symbols(classification))
        except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
            violations.append(
                _violation(
                    "legacy_classification_unreadable",
                    table="capital_snapshots",
                    account_id=account,
                    environment=environment,
                )
            )
    claims = await session.execute(
        text(
            "SELECT DISTINCT exchange_account_id, deployment_environment, symbol "
            f"FROM public.offer_claims WHERE {_OPEN_CLAIMS}"
        )
    )
    live.update((account, environment, symbol) for account, environment, symbol in claims.all())
    violations.extend(
        _violation(
            "live_symbol_without_cell",
            account_id=account,
            environment=environment,
            symbol=symbol,
        )
        for account, environment, symbol in sorted(live)
        if (account, environment) in listed and (account, environment, symbol) not in cells
    )

    ordered = tuple(sorted(violations, key=lambda item: tuple(sorted(item.items()))))
    return InventoryResult("not_comparable" if ordered else "ok", ordered)
