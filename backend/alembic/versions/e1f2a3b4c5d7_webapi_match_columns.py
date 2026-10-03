"""Web API match-preview columns: stored generated match terms and column-level offer grants.

Under the ledger authority the web API previews an UNKNOWN attempt's match against the
latest accepted observation, as the legacy path does. The matcher needs the attempt's
terms (``normalized_payload``), the observation's declared history symbols and page
counts (``evidence``) and the observed offers, none of which the web API may read whole.
So the terms and the two evidence keys become STORED generated columns (as
``intended_amount`` is) and ``bfx_webapi`` is granted exactly those columns plus the
match columns of the observation, observed-offer and offer-history tables. Never
``raw``, ``evidence``, ``normalized_payload`` or ``scope_block``.

``match_rate`` / ``match_period_days`` cast the payload text, so a present-but-unparsable
value is refused at write; the upgrade refuses when an existing row already holds one
(an absent key is NULL: a seeded partial is simply not a subject). ``match_offer_type`` /
``match_flags`` and the evidence columns select by JSON type and cannot fail.

Allowlist: ``WEBAPI_TABLE_PRIVILEGES`` / ``WEBAPI_COLUMN_PRIVILEGES`` below are the newest
copy of the exact allowlist ``d0e1f2a3b4c6`` introduced (its docstring says how it is
kept); ``tests/integration/test_webapi_privilege_allowlist.py`` compares the effective
privileges at head with this copy. Downgrade revokes only what this revision granted.

Revision ID: e1f2a3b4c5d7
Revises: d0e1f2a3b4c6
"""

from sqlalchemy import text

from alembic import op

revision = "e1f2a3b4c5d7"
down_revision = "d0e1f2a3b4c6"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ROLE = "bfx_webapi"
_ATTEMPT = "submission_attempt_journal"
_OBSERVATION = "ledger_observation"
_RATE_CHECK = "ck_submission_attempt_match_rate"

# Expressions are the model's (``modules/ledger/tables.py``); a test compares the stored ones.
ATTEMPT_GENERATED: dict[str, tuple[str, str]] = {
    "match_rate": ("numeric", "(normalized_payload->>'rate')::numeric"),
    "match_period_days": ("integer", "(normalized_payload->>'period')::integer"),
    "match_offer_type": (
        "text",
        "CASE WHEN jsonb_typeof(normalized_payload->'type') = 'string' "
        "THEN normalized_payload->>'type' END",
    ),
    "match_flags": (
        "jsonb",
        "CASE WHEN jsonb_typeof(normalized_payload->'flags') IN ('object', 'number') "
        "THEN normalized_payload->'flags' END",
    ),
}
OBSERVATION_GENERATED: dict[str, tuple[str, str]] = {
    "history_symbols": (
        "jsonb",
        "CASE WHEN jsonb_typeof(evidence->'history_symbols') = 'array' "
        "THEN evidence->'history_symbols' END",
    ),
    "first_page_counts": (
        "jsonb",
        "CASE WHEN jsonb_typeof(evidence->'first_page_counts') = 'object' "
        "THEN evidence->'first_page_counts' END",
    ),
}
ATTEMPT_MATCH_COLUMNS = tuple(ATTEMPT_GENERATED)
OBSERVATION_MATCH_COLUMNS = (
    "history_requested_start_ms", "history_requested_end_ms", "history_oldest_mts_created",
    "history_newest_mts_created", "offer_history_pages", "credit_history_pages",
    "trades_requested_start_ms", "trades_requested_end_ms", *OBSERVATION_GENERATED,
)
OBSERVED_OFFER_MATCH_COLUMNS = (
    "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining",
    "rate", "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created",
    "mts_updated",
)

_RW = ("DELETE", "INSERT", "SELECT", "UPDATE")
_R = ("SELECT",)

WEBAPI_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "account_config_drafts": _RW,
    "api_keys": _RW,
    "attribution_weekly": _R,
    "capital_authority_epoch": _R,
    "capital_policy_heads": _R,
    "capital_policy_requests": _R,
    "capital_policy_revisions": _R,
    "deployments": _R,
    "event_log": _R,
    "exchange_account_credentials": ("INSERT", "SELECT", "UPDATE"),
    "exchange_account_memberships": _R,
    "exchange_accounts": _R,
    "execution_uncertainties": _R,
    "funding_cancel_all_audit": _R,
    "funding_candles": _R,
    "funding_credit_history": _R,
    "funding_interest_payments": _R,
    "funding_trades": _R,
    "offer_claims": _R,
    "position_state": _R,
    "submission_attempts": _R,
    "trading_control_requests": _R,
    "trading_state": _R,
    "uncertainty_resolution_requests": _R,
    "user_configs": _RW,
    "user_profiles": ("INSERT", "SELECT", "UPDATE"),
}

# (table, privilege) -> columns. Only columns whose table has no table-level grant of
# that privilege; never ``evidence``, ``raw``, ``scope_block`` or ``normalized_payload``.
WEBAPI_COLUMN_PRIVILEGES: dict[tuple[str, str], tuple[str, ...]] = {
    ("accepted_capital_basis", "SELECT"): (
        "id", "exchange_account_id", "deployment_environment", "observation_id",
        "accept_revision", "attempt_seq_high_water", "accepted_at_ms",
    ),
    ("accepted_capital_basis_attempt", "SELECT"): (
        "basis_id", "attempt_id", "symbol", "classification",
    ),
    ("accepted_capital_basis_credit", "SELECT"): ("basis_id", "symbol"),
    ("accepted_capital_basis_quarantine", "SELECT"): ("basis_id", "quarantine_id"),
    ("accepted_capital_basis_symbol", "SELECT"): (
        "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits",
    ),
    ("alembic_version", "SELECT"): ("version_num",),
    ("capital_policy_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms",
    ),
    ("execution_resolution_journal", "SELECT"): (
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "actor_kind", "actor_id", "operator_request_id",
        "resolved_at_ms", "reason",
    ),
    ("ledger_observation", "SELECT"): (
        "id", "query_id", "exchange_account_id", "deployment_environment", "accepted",
        "query_finished_at_ms", "first_digest", "confirmation_digest",
        "wallets_complete", "offers_complete", "credits_complete", "loans_complete",
        "offer_history_complete", "credit_history_complete", "trades_complete",
        *OBSERVATION_MATCH_COLUMNS,
    ),
    ("ledger_observation_offer", "SELECT"): OBSERVED_OFFER_MATCH_COLUMNS,
    ("ledger_observation_offer_history", "SELECT"): (
        *OBSERVED_OFFER_MATCH_COLUMNS, "terminal_kind", "occurred_at_ms",
    ),
    ("ledger_observation_query", "SELECT"): (
        "query_id", "exchange_account_id", "deployment_environment", "query_revision",
        "started_at_ms",
    ),
    ("quarantine_opening", "SELECT"): (
        "quarantine_id", "exchange_account_id", "deployment_environment", "symbol",
        "intended_amount", "opened_at_ms", "opened_revision", "source_attempt_id",
    ),
    ("submission_attempt_journal", "SELECT"): (
        "attempt_id", "execution_decision_id", "exchange_account_id", "deployment_environment",
        "symbol", "cell_id", "attempt_seq", "started_at_ms", "intended_amount",
        *ATTEMPT_MATCH_COLUMNS,
    ),
    ("trading_control_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms",
    ),
    ("transport_outcome_journal", "SELECT"): (
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms",
    ),
    ("uncertainty_resolution_requests", "INSERT"): (
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id",
        "venue_offer_id", "action", "decision", "reason", "requested_by", "created_at_ms",
        "reconcile_event_seq", "observation_id",
    ),
    ("venue_offer_mirror", "SELECT"): (
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    ),
}


# What this revision grants beyond ``d0e1f2a3b4c6``'s allowlist.
_GRANTED: dict[tuple[str, str], tuple[str, ...]] = {
    (_ATTEMPT, "SELECT"): ATTEMPT_MATCH_COLUMNS,
    (_OBSERVATION, "SELECT"): OBSERVATION_MATCH_COLUMNS,
    ("ledger_observation_offer", "SELECT"): OBSERVED_OFFER_MATCH_COLUMNS,
    ("ledger_observation_offer_history", "SELECT"): (
        *OBSERVED_OFFER_MATCH_COLUMNS, "terminal_kind", "occurred_at_ms",
    ),
}

# A present rate must be a finite numeric, a present period an integer; absent is fine.
_BAD_TERMS = """
    SELECT count(*) FROM public.submission_attempt_journal j
    WHERE (j.normalized_payload->>'rate' IS NOT NULL
           AND (NOT pg_input_is_valid(j.normalized_payload->>'rate', 'numeric')
                OR CASE WHEN pg_input_is_valid(j.normalized_payload->>'rate', 'numeric')
                        THEN NOT ((j.normalized_payload->>'rate')::numeric < 'Infinity'::numeric)
                        ELSE true END))
       OR (j.normalized_payload->>'period' IS NOT NULL
           AND NOT pg_input_is_valid(j.normalized_payload->>'period', 'integer'))
"""


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _add(table: str, columns: dict[str, tuple[str, str]]) -> None:
    for name, (sql_type, expression) in columns.items():
        op.execute(
            f"ALTER TABLE public.{table} ADD COLUMN {name} {sql_type} "
            f"GENERATED ALWAYS AS ({expression}) STORED"
        )


def upgrade() -> None:
    bad = op.get_bind().execute(text(_BAD_TERMS)).scalar()
    if bad:
        raise RuntimeError(
            f"refuse upgrade: {bad} {_ATTEMPT} rows have a payload rate or period that cannot be read"
        )
    _add(_ATTEMPT, ATTEMPT_GENERATED)
    op.execute(
        f"ALTER TABLE public.{_ATTEMPT} ADD CONSTRAINT {_RATE_CHECK} "
        "CHECK (match_rate IS NULL OR match_rate < 'Infinity'::numeric)"
    )
    _add(_OBSERVATION, OBSERVATION_GENERATED)
    if _role_exists(_ROLE):
        for (table, privilege), columns in _GRANTED.items():
            op.execute(f"GRANT {privilege} ({', '.join(columns)}) ON public.{table} TO {_ROLE}")


def downgrade() -> None:
    if _role_exists(_ROLE):
        for (table, privilege), columns in _GRANTED.items():
            op.execute(f"REVOKE {privilege} ({', '.join(columns)}) ON public.{table} FROM {_ROLE}")
    for name in OBSERVATION_GENERATED:
        op.execute(f"ALTER TABLE public.{_OBSERVATION} DROP COLUMN {name}")
    op.execute(f"ALTER TABLE public.{_ATTEMPT} DROP CONSTRAINT {_RATE_CHECK}")
    for name in ATTEMPT_GENERATED:
        op.execute(f"ALTER TABLE public.{_ATTEMPT} DROP COLUMN {name}")
