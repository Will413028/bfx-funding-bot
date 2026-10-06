"""Weekly attribution: the legacy offer -> cell, resolved once out of the frozen tables (S1-8 D4a).

The weekly attribution recomputes every week from all history, so which cell placed each
pre-switch offer is a permanent input. The legacy authority recorded it only indirectly: an
offer's execution decision or signal correlation id in ``offer_claims``, ``venue_offer_state``
and the ``ORDER_FILL`` events of ``event_log`` (frozen since the switch, ``e8f9a0b1c2d3``, and
leaving ``public`` in S1-8), resolved through ``execution_decisions`` and the ``diagnostics``
DECISION rows (non-SoT, prunable). This revision resolves it once into
``attribution_legacy_offer_cells`` -- one row per (scope, venue offer, cell) -- and the loader
reads only that table for legacy offers.

Resolution (a frozen copy of the loader's pre-S1-8 rule, applied per scope):

* a link (one source row) resolves through its execution decision's cell; without one, through
  its signal correlation id: the cells of the ``execution_decisions`` with that id, or, when no
  decision has it, the cells of the ``diagnostics`` DECISION rows naming it;
* an offer takes the cells its links resolve to through a decision; when none does, the cells
  they resolve to through the signal correlation id (the fallback for offers placed before
  decisions were recorded);
* more than one cell is a conflict and every cell is written: the loader reports the offer
  (``OFFER CELL CONFLICTS``) and leaves its credits ``unattributed``, as it does when journal
  attempts disagree. The loader used to pick one by row order; with one cell per offer the
  result is the same;
* an offer no link resolves is not written (foreign, as before); a link without a venue offer
  id never matched a funding trade (``offer_id`` is NOT NULL) and is skipped; rows of a NULL
  ``exchange_account_id`` (legacy-only test realms) are skipped: the weekly reads by UUID.

The copy refuses (``RuntimeError``) unless the source is frozen: when any source row exists,
the latest ``capital_authority_epoch`` must be ``ledger``. Empty legacy tables (CI, a fresh
host) are a no-op under any epoch. Deterministic and idempotent (natural key, ``ON CONFLICT DO
NOTHING``).

Grants: owner-written only; ``bfx_bot`` (the weekly job's role) gets SELECT, ``bfx_webapi``,
``bfx_webauth`` and ``bfx_cutover_reader`` nothing (the web API allowlist is unchanged). The
table carries the realm guard like every ``deployment_environment`` table.

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection

from alembic import op

revision = "a0b1c2d3e4f5"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

TABLE = "attribution_legacy_offer_cells"
# Every table with a ``deployment_environment`` column this revision adds; the realm coverage
# test adds them to ``a7c3e9f1b2d4``'s list.
REALM_TABLES = (f"public.{TABLE}",)
_READER = "bfx_bot"
_NO_ACCESS = ("bfx_webapi", "bfx_webauth", "bfx_cutover_reader")

# venue offer id, execution decision id, signal correlation id
Link = tuple[str, str | None, str | None]
Scope = tuple[Any, str]

_CLAIMS = (
    "SELECT exchange_account_id, deployment_environment, venue_offer_id, execution_decision_id, "
    "signal_correlation_id FROM public.offer_claims "
    "WHERE exchange_account_id IS NOT NULL AND venue_offer_id IS NOT NULL"
)
_VENUE_OFFERS = (
    "SELECT exchange_account_id, deployment_environment, venue_offer_id, execution_decision_id, "
    "signal_correlation_id FROM public.venue_offer_state"
)
_FILLS = (
    "SELECT exchange_account_id, deployment_environment, payload->'venue_offer_id', "
    "venue_offer_id, payload->'signal_correlation_id' FROM public.event_log "
    "WHERE event_type = 'ORDER_FILL' AND exchange_account_id IS NOT NULL"
)


def _role_exists(conn: Connection, name: str) -> bool:
    return bool(
        conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name}).scalar()
    )


def resolve_offer_cells(
    links: Iterable[Link],
    decisions: Iterable[tuple[str, str | None, str | None]],
    diagnostics: Iterable[tuple[Any, Any]],
) -> dict[str, frozenset[str]]:
    """venue offer id -> its cells (one, or several for a conflict), for one scope.

    ``decisions``: (decision id, signal correlation id, cell); ``diagnostics``: the DECISION
    payloads' (correlation_id, cell) values as stored (JSON-decoded)."""
    decisions = list(decisions)
    cell_by_decision = {decision: cell for decision, _scid, cell in decisions}
    by_scid_decision: dict[Any, set[Any]] = defaultdict(set)
    for _decision, scid, cell in decisions:
        by_scid_decision[scid].add(cell)
    by_scid_diagnostics: dict[str, set[str]] = defaultdict(set)
    for correlation, cell in diagnostics:
        if correlation and cell:
            by_scid_diagnostics[str(correlation)].add(str(cell))

    def by_scid(scid: str) -> set[str]:
        if scid in by_scid_decision:  # a decision's correlation overrides the diagnostics'
            cells: set[Any] = by_scid_decision[scid]
        else:
            cells = by_scid_diagnostics.get(scid, set())
        return {c for c in cells if c}

    through_decision: dict[str, set[str]] = defaultdict(set)
    through_scid: dict[str, set[str]] = defaultdict(set)
    for offer, decision, scid in links:
        if not offer:
            continue
        cell = cell_by_decision.get(decision or "")
        if cell:
            through_decision[offer].add(cell)
        else:
            through_scid[offer] |= by_scid(scid or "")
    out = {}
    for offer in set(through_decision) | set(through_scid):
        cells = through_decision.get(offer) or through_scid.get(offer)
        if cells:
            out[offer] = frozenset(cells)
    return out


def _fill_link(payload_offer: Any, column_offer: Any, payload_scid: Any) -> Link:
    """The link the loader took from one ORDER_FILL (offer id from the payload, else the column)."""
    return str(payload_offer or column_offer or ""), None, str(payload_scid or "") or None


def _by_scope(conn: Connection) -> Mapping[Scope, list[Link]]:
    links: dict[Scope, list[Link]] = defaultdict(list)
    for scope, env, offer, decision, scid in conn.execute(text(_CLAIMS)):
        links[(scope, env)].append((str(offer), decision, scid))
    for scope, env, offer, decision, scid in conn.execute(text(_VENUE_OFFERS)):
        links[(scope, env)].append((offer, decision, scid))
    for scope, env, payload_offer, column_offer, payload_scid in conn.execute(text(_FILLS)):
        links[(scope, env)].append(_fill_link(payload_offer, column_offer, payload_scid))
    return links


def _require_frozen_source(conn: Connection, has_rows: bool) -> None:
    if not has_rows:
        return
    authority = conn.execute(text(
        "SELECT authority FROM public.capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1"
    )).scalar()
    if authority != "ledger":
        raise RuntimeError(
            f"{revision}: the legacy offer tables hold rows but the capital authority is "
            f"{authority!r}, not 'ledger'; the attribution copy is taken once from the frozen "
            "legacy tables, so switch the authority first (docs/runbooks/ledger-switch.md)"
        )


def materialize(conn: Connection) -> None:
    """Resolve and copy the legacy offer -> cell (idempotent; owner connection)."""
    links = _by_scope(conn)
    _require_frozen_source(conn, any(links.values()))
    rows = []
    for (scope, env), scope_links in sorted(links.items(), key=lambda i: (str(i[0][0]), i[0][1])):
        decisions = [tuple(r) for r in conn.execute(text(
            "SELECT decision_id, signal_correlation_id, cell_id FROM public.execution_decisions "
            "WHERE exchange_account_id = :scope AND deployment_environment = :env"
        ), {"scope": scope, "env": env})]
        diagnostics = [tuple(r) for r in conn.execute(text(
            "SELECT payload->'correlation_id', payload->'cell' FROM public.diagnostics "
            "WHERE kind = 'decision' AND exchange_account_id = :scope "
            "AND deployment_environment = :env"
        ), {"scope": scope, "env": env})]
        cells = resolve_offer_cells(scope_links, decisions, diagnostics)
        rows += [{"scope": scope, "env": env, "offer": offer, "cell": cell}
                 for offer in sorted(cells) for cell in sorted(cells[offer])]
    if rows:
        conn.execute(text(
            f"INSERT INTO public.{TABLE} (exchange_account_id, deployment_environment, "
            "venue_offer_id, cell) VALUES (:scope, :env, :offer, :cell) ON CONFLICT DO NOTHING"
        ), rows)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("exchange_account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("cell", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "exchange_account_id", "deployment_environment", "venue_offer_id", "cell"),
        sa.CheckConstraint("venue_offer_id <> '' AND cell <> ''", name=f"ck_{TABLE}_not_empty"),
    )
    conn = op.get_bind()
    for name in REALM_TABLES:
        op.execute(
            f"CREATE TRIGGER database_realm_write BEFORE INSERT OR UPDATE OF "
            f"deployment_environment ON {name} FOR EACH ROW "
            "EXECUTE FUNCTION public.guard_database_realm()"
        )
    op.execute(f"REVOKE ALL ON public.{TABLE} FROM PUBLIC")
    for role in _NO_ACCESS:
        if _role_exists(conn, role):
            op.execute(f"REVOKE ALL ON public.{TABLE} FROM {role}")
    if _role_exists(conn, _READER):
        op.execute(f"REVOKE ALL ON public.{TABLE} FROM {_READER}")
        op.execute(f"GRANT SELECT ON public.{TABLE} TO {_READER}")
    materialize(conn)


def downgrade() -> None:
    op.drop_table(TABLE)
