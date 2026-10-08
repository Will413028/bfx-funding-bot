"""5e820d6dc7da on real PostgreSQL roles: an operator request's effects name it, in its scope.

The trading state, policy revision and cancel-all audit a request causes carry its id under a
foreign key that includes the request's scope (and currency); a journal row's link to its
request includes the uncertainty it resolves. The request keeps its state columns: the column
grants keep what the operator asked fixed, a trigger lets ``state`` leave ``requested`` once.
The request's product columns close (no UPDATE grant, no CHECK names them) for the next
release to drop. The upgrade back-fills the links after checking the rows can be linked.

Mutation checks (one at a time; revert after each):

* Drop the scope columns from any composite FK (migration and model): its case of
  ``test_an_effect_names_only_a_request_of_its_own_scope`` fails.
* Drop the G2 block from the uncertainty guard: ``test_the_request_guards_keep_the_transition_and_g2``
  fails.
* Revert G1 to read ``source ->> 'request_id'``: ``test_g1_authorizes_by_the_typed_request``
  fails.
* Skip the trading or capital back-fill, or drop the reason from ``_TRADING_MATCH``: the
  upgrade raises at a postcondition, or the row-by-row comparison fails.
* Back-fill the cancel-all audit from the earliest attempt only: the two-currency kill
  compares wrong.
* Drop any ``ENABLE TRIGGER``: the upgrade raises (append-only trigger left disabled).
* Drop any entry of ``PRECONDITIONS``: its case of the precondition test fails.
* Keep the old ``ck_*_outcome`` (naming the product column): the CHECK test fails.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.pg_templates import LAST_REVERSIBLE_REVISION, alembic, template_at

from .test_ledger_schema_roles import _A, _O, _P, _T, _build, _build_reversible, _prepare, _seed
from .test_uncertainty_request_evidence import _quarantine

pytestmark = pytest.mark.integration

_OTHER = "00000000-0000-0000-0000-00000000b0b0"
_REVISION = "5e820d6dc7da"

# What the previous web API image (6c775e3b) selects from each request table: every column its
# models mapped (`git show 6c775e3b:backend/src/bfx_funding_bot/modules/execution/...`).
_PREVIOUS_WEBAPI_COLUMNS = {
    "trading_control_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "action", "reason",
        "requested_by", "created_at_ms", "state", "processed_at_ms", "outcome_reason",
        "trading_state_id"),
    "capital_policy_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "symbol", "action",
        "reason", "requested_by", "created_at_ms", "state", "processed_at_ms", "outcome_reason",
        "policy_revision_id"),
    "uncertainty_resolution_requests": (
        "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id", "action",
        "observation_id", "venue_offer_id", "decision", "reason", "requested_by", "created_at_ms",
        "state", "processed_at_ms", "outcome_reason"),
    "trading_state": (
        "id", "exchange_account_id", "deployment_environment", "state", "cause", "actor", "reason",
        "created_at_ms", "legacy_halt_id"),
    "funding_cancel_all_audit": (
        "id", "exchange_account_id", "deployment_environment", "trading_state_id", "attempt_id",
        "currency", "phase", "venue_status", "detail", "actor", "occurred_at_ms"),
    "capital_policy_revisions": (
        "id", "exchange_account_id", "deployment_environment", "symbol", "revision",
        "schema_version", "policy", "digest", "source"),
}


@pytest.fixture
def head(pg_templates, pg_clone):
    """Head with production-like grants and one row per ledger table."""
    yield from _seeded(pg_clone(pg_templates.template("ledger_s1_roles", _build)))


@pytest.fixture
def at_revision(pg_templates, pg_clone):
    """5e820d6dc7da itself: the product columns are still in the table (41cec7caf291 drops
    them), and this is the schema the previous web API image ran against."""
    yield from _seeded(pg_clone(pg_templates.template(
        "ledger_s1_roles_causation", template_at(_REVISION, _prepare))))


@pytest.fixture
def before(pg_templates, pg_clone):
    """The revision before 5e820d6dc7da, seeded the same way: the upgrade runs in the test."""
    yield from _seeded(pg_clone(pg_templates.template("ledger_s1_roles_reversible", _build_reversible)))


def _seeded(url: str):
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed(conn)
        conn.exec_driver_sql(
            f"INSERT INTO exchange_accounts(id, venue, label) VALUES ('{_OTHER}', 'bitfinex', 'other')")
    try:
        yield url, engine
    finally:
        engine.dispose()


# ------------------------------------------------------------------ rows, as the owner


def _state(conn, *, state: str = "HALTED", cause: str = "operator", actor: str = "operator",
           reason: str = "test", at: int = 1, account: str = _A, request: str | None = None) -> int:
    request_column, request_value = ("", "") if request is None else (
        ", operator_request_id", f", '{request}'")
    return conn.scalar(text(
        "INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause, actor, "
        f"reason, created_at_ms{request_column}) VALUES (:a, 'ci', :s, :c, :actor, :r, :at"
        f"{request_value}) RETURNING id"),
        {"a": account, "s": state, "c": cause, "actor": actor, "r": reason, "at": at})


def _trading(conn, *, action: str = "resume", reason: str = "x", by: str = "operator",
             created: int = 1, account: str = _A, settle: str = "") -> str:
    request_id = str(uuid4())
    conn.exec_driver_sql(
        "INSERT INTO trading_control_requests (request_id, exchange_account_id, deployment_environment, "
        f"action, reason, requested_by, created_at_ms) VALUES ('{request_id}', '{account}', 'ci', "
        f"'{action}', '{reason}', '{by}', {created})")
    if settle:
        conn.exec_driver_sql(f"UPDATE trading_control_requests SET {settle} WHERE request_id = '{request_id}'")
    return request_id


def _capital(conn, *, action: str = "disable", symbol: str = "fUST", settle: str = "") -> str:
    request_id = str(uuid4())
    conn.exec_driver_sql(
        "INSERT INTO capital_policy_requests (request_id, exchange_account_id, deployment_environment, "
        f"symbol, action, reason, requested_by, created_at_ms) VALUES ('{request_id}', '{_A}', 'ci', "
        f"'{symbol}', '{action}', 'x', 'operator', 1)")
    if settle:
        conn.exec_driver_sql(f"UPDATE capital_policy_requests SET {settle} WHERE request_id = '{request_id}'")
    return request_id


def _revision(conn, *, revision: int, source: str = "{}", symbol: str = "fUST",
              request: str | None = None) -> str:
    revision_id = str(uuid4())
    request_column, request_value = ("", "") if request is None else (
        ", operator_request_id", f", '{request}'")
    conn.exec_driver_sql(
        "INSERT INTO capital_policy_revisions (id, exchange_account_id, deployment_environment, symbol, "
        f"revision, schema_version, policy, digest, source{request_column}) VALUES ('{revision_id}', "
        f"'{_A}', 'ci', '{symbol}', {revision}, 1, '{{}}', 'p', '{source}'{request_value})")
    return revision_id


def _uncertainty(conn, *, uncertainty: str | None = None, settle: str = "") -> str:
    request_id = str(uuid4())
    conn.exec_driver_sql(
        "INSERT INTO uncertainty_resolution_requests (request_id, exchange_account_id, "
        "deployment_environment, uncertainty_id, action, observation_id, requested_by, created_at_ms) "
        f"VALUES ('{request_id}', '{_A}', 'ci', '{uncertainty or uuid4()}', 'mark_not_accepted', "
        f"'{_O}', 'operator', 1)")
    if settle:
        conn.exec_driver_sql(
            f"UPDATE uncertainty_resolution_requests SET {settle} WHERE request_id = '{request_id}'")
    return request_id


def _journal(conn, request: str | None, *, quarantine: str | None = None,
             attempt: str | None = None, account: str = _A) -> None:
    subject_column, subject = ("attempt_id", attempt) if attempt else (
        "quarantine_id", quarantine or _quarantine(conn))
    request_sql = "NULL" if request is None else f"'{request}'"
    conn.exec_driver_sql(
        f"""INSERT INTO execution_resolution_journal(id, {subject_column}, exchange_account_id,
      deployment_environment, symbol, action, observation_id, actor_kind, actor_id,
      operator_request_id, resolved_at_ms, reason, evidence)
      VALUES ('{uuid4()}', '{subject}', '{account}', 'ci', 'fUST', 'not_accepted', '{_O}',
      'operator', 'test', {request_sql}, 6, 'test', '{{}}')""")


def _resolved_uncertainty(conn, settle: str = "") -> str:
    """A request and the journal row resolving the quarantine it names."""
    quarantine = _quarantine(conn)
    request = _uncertainty(conn, uncertainty=quarantine)
    _journal(conn, request, quarantine=quarantine)
    if settle:
        conn.exec_driver_sql(
            f"UPDATE uncertainty_resolution_requests SET {settle} WHERE request_id = '{request}'")
    return request


def _audit(conn, *, state_id: int, actor: str, currency: str, at: int, account: str = _A,
           request: str | None = None) -> str:
    """One cancel-all attempt: its requested row and its outcome row."""
    attempt = str(uuid4())
    request_column, request_value = ("", "") if request is None else (
        ", operator_request_id", f", '{request}'")
    for phase, offset in (("requested", 0), ("acknowledged", 1)):
        conn.exec_driver_sql(
            "INSERT INTO funding_cancel_all_audit (exchange_account_id, deployment_environment, "
            f"trading_state_id, attempt_id, currency, phase, actor, occurred_at_ms{request_column}) "
            f"VALUES ('{account}', 'ci', {state_id}, '{attempt}', '{currency}', '{phase}', '{actor}', "
            f"{at + offset}{request_value})")
    return attempt


def _refused(engine, sql: str, match: str, *, role: str | None = None) -> None:
    with pytest.raises(DBAPIError, match=match), engine.begin() as conn:
        if role is not None:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
        conn.exec_driver_sql(sql)


# ------------------------------------------------------------------ the links, at head


def test_an_effect_names_only_a_request_of_its_own_scope(head) -> None:
    _, engine = head
    with engine.begin() as conn:
        other_scope = _trading(conn, account=_OTHER)
        fusd = _capital(conn, symbol="fUSD")
        mine = _trading(conn)
        other_state = _state(conn, account=_OTHER)
        unrelated = _uncertainty(conn)  # names an uncertainty nobody resolves here
    with engine.begin() as conn, pytest.raises(IntegrityError, match="fk_trading_state_operator_request"):
        _state(conn, request=other_scope)
    with engine.begin() as conn, pytest.raises(
            IntegrityError, match="fk_capital_policy_revisions_operator_request"):
        _revision(conn, revision=2, request=fusd)  # fUST revision, fUSD request
    with engine.begin() as conn, pytest.raises(
            IntegrityError, match="fk_funding_cancel_all_audit_operator_request"):
        _audit(conn, state_id=_state(conn, account=_OTHER), actor="operator", currency="UST",
               at=1, account=_OTHER, request=mine)
    with engine.begin() as conn, pytest.raises(
            IntegrityError, match="fk_funding_cancel_all_audit_trading_state"):
        _audit(conn, state_id=other_state, actor="operator", currency="UST", at=1)
    with engine.begin() as conn, pytest.raises(
            IntegrityError, match="fk_execution_resolution_journal_request_quarantine"):
        _journal(conn, unrelated)
    with engine.begin() as conn, pytest.raises(
            IntegrityError, match="fk_execution_resolution_journal_request_attempt"):
        _journal(conn, unrelated, attempt=_T)
    with engine.begin() as conn:  # the same rows in scope link fine
        _state(conn, request=mine)
        _audit(conn, state_id=_state(conn), actor="operator", currency="UST", at=1, request=mine)
        _revision(conn, revision=2, request=_capital(conn))
        _journal(conn, _uncertainty(conn, uncertainty=_T), attempt=_T)
        _resolved_uncertainty(conn)


def test_a_request_has_at_most_one_effect(head) -> None:
    _, engine = head
    with engine.begin() as conn:
        trading, capital = _trading(conn), _capital(conn)
        _state(conn, request=trading)
        _revision(conn, revision=2, request=capital)
    with engine.begin() as conn, pytest.raises(IntegrityError, match="uq_trading_state_operator_request"):
        _state(conn, request=trading)
    with engine.begin() as conn, pytest.raises(
            IntegrityError, match="uq_capital_policy_revisions_operator_request"):
        _revision(conn, revision=3, request=capital)


def test_the_effects_stay_append_only(head) -> None:
    _, engine = head
    with engine.begin() as conn:
        state_id = _state(conn, request=_trading(conn))
        _audit(conn, state_id=state_id, actor="operator", currency="UST", at=1)
    for sql, message in (
            ("UPDATE trading_state SET operator_request_id = NULL", "immutable trading state history"),
            ("UPDATE capital_policy_revisions SET operator_request_id = NULL", "immutable"),
            ("UPDATE funding_cancel_all_audit SET operator_request_id = NULL",
             "immutable funding cancel-all audit"),
    ):
        _refused(engine, sql, message)


def test_the_request_guards_keep_the_transition_and_g2(head) -> None:
    """Every UPDATE is the one transition out of ``requested``, for every role. What the operator
    asked is the grants' to keep: the owner's transition may rewrite it (D8 accepts that)."""
    _, engine = head
    with engine.begin() as conn:
        settled = _trading(conn, settle="state='rejected', processed_at_ms=2, outcome_reason='x'")
        waiting = _trading(conn, action="kill")
        unresolved = _uncertainty(conn)
    _refused(engine, f"UPDATE trading_control_requests SET outcome_reason = 'y' WHERE request_id = '{settled}'",
             "invalid trading control request transition")
    _refused(engine, f"UPDATE trading_control_requests SET reason = 'owner' WHERE request_id = '{waiting}'",
             "invalid trading control request transition")
    _refused(engine, "UPDATE trading_control_requests SET reason = 'bot', state = 'rejected', "
             f"processed_at_ms = 2, outcome_reason = 'x' WHERE request_id = '{waiting}'",
             "permission denied", role="bfx_bot")
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE trading_control_requests SET reason = 'owner', state = 'rejected', "
                             f"processed_at_ms = 2, outcome_reason = 'x' WHERE request_id = '{waiting}'")
    _refused(engine, "UPDATE uncertainty_resolution_requests SET state='applied', processed_at_ms=2 "
             f"WHERE request_id = '{unresolved}'", "applied without a journal row", role="bfx_bot")


def test_the_product_columns_are_closed(at_revision) -> None:
    _, engine = at_revision
    with engine.begin() as conn:
        state_id = _state(conn)
        trading = _trading(conn, action="kill")
        capital = _capital(conn)
        for table, column in (("trading_control_requests", "trading_state_id"),
                              ("capital_policy_requests", "policy_revision_id")):
            assert not conn.scalar(text(
                f"SELECT has_column_privilege('bfx_bot', 'public.{table}', '{column}', 'UPDATE')"))
            check = conn.scalar(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                f"WHERE conname = 'ck_{table}_outcome'"))
            assert column not in check
    _refused(engine, f"UPDATE trading_control_requests SET trading_state_id = {state_id} "
             f"WHERE request_id = '{trading}'", "permission denied", role="bfx_bot")
    _refused(engine, f"UPDATE capital_policy_requests SET policy_revision_id = '{_P}' "
             f"WHERE request_id = '{capital}'", "permission denied", role="bfx_bot")
    # The state shape still holds without them.
    _refused(engine, "UPDATE trading_control_requests SET state = 'rejected', processed_at_ms = 2 "
             f"WHERE request_id = '{trading}'", "ck_trading_control_requests_outcome", role="bfx_bot")
    with engine.begin() as conn:  # applied needs no product any more
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE capital_policy_requests SET state='applied', processed_at_ms=2, "
                             f"outcome_reason='unchanged' WHERE request_id = '{capital}'")


def test_the_previous_web_api_still_reads_what_it_maps(at_revision) -> None:
    """The compatibility window: the 6c775e3b web API runs against this schema until it is
    replaced, and ``select(Model)`` names every column it mapped."""
    _, engine = at_revision
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        for table, columns in _PREVIOUS_WEBAPI_COLUMNS.items():
            conn.exec_driver_sql(f"SELECT {', '.join(columns)} FROM {table}")


# ------------------------------------------------------------------ the upgrade


def _history(conn) -> dict[str, object]:
    """Operator history with distinct clock reads for every row, on the revision before.

    The worker reads its clock for ``processed_at_ms`` before it writes the trading state, so
    the state a request wrote is never older than its processing (here 1 ms or more later)."""
    ids: dict[str, object] = {}

    def applied(request: str, state_id: int, processed: int) -> None:
        conn.exec_driver_sql(
            f"UPDATE trading_control_requests SET state='applied', processed_at_ms={processed}, "
            f"trading_state_id={state_id} WHERE request_id='{request}'")

    # A kill writes its HALTED; its cancel-all covers two currencies.
    ids["k1"] = _trading(conn, action="kill", reason="a", created=90)
    ids["s1"] = _state(conn, reason="kill: a", at=100)
    applied(ids["k1"], ids["s1"], 99)
    ids["k1_ust"] = _audit(conn, state_id=ids["s1"], actor="operator", currency="UST", at=106)
    ids["k1_usd"] = _audit(conn, state_id=ids["s1"], actor="operator", currency="USD", at=108)
    # A resume.
    ids["r1"] = _trading(conn, reason="r", created=140)
    ids["s2"] = _state(conn, state="ACTIVE", reason="resumed: r", at=150)
    applied(ids["r1"], ids["s2"], 149)
    # An automatic halt, then a kill over it (an operator halt over an automatic one is written).
    ids["s3"] = _state(conn, cause="auto", actor="auto", reason="loss", at=160)
    ids["k4"] = _trading(conn, action="kill", reason="b", created=165)
    ids["s4"] = _state(conn, reason="kill: b", at=170)
    applied(ids["k4"], ids["s4"], 169)
    ids["k4_ust"] = _audit(conn, state_id=ids["s4"], actor="operator", currency="UST", at=176)
    # The same kill asked again: it restates s4 and retries the cancel-all.
    ids["k2"] = _trading(conn, action="kill", reason="b", created=180)
    applied(ids["k2"], ids["s4"], 185)
    ids["k2_ust"] = _audit(conn, state_id=ids["s4"], actor="operator", currency="UST", at=186)
    # Resume, then /admin/halt, then a kill that restates it.
    ids["r2"] = _trading(conn, reason="q", created=188)
    ids["s5a"] = _state(conn, state="ACTIVE", reason="resumed: q", at=190)
    applied(ids["r2"], ids["s5a"], 189)
    ids["s5"] = _state(conn, actor="admin-api", reason="manual", at=200)
    ids["admin_ust"] = _audit(conn, state_id=ids["s5"], actor="admin-api", currency="UST", at=201)
    ids["k3"] = _trading(conn, action="kill", reason="c", created=205)
    applied(ids["k3"], ids["s5"], 210)
    ids["k3_ust"] = _audit(conn, state_id=ids["s5"], actor="operator", currency="UST", at=211)
    # /admin/halt run with the operator's own id as its actor: only the reason tells it from a
    # kill, and the kill that restates it wrote nothing.
    ids["r3"] = _trading(conn, reason="z", created=212)
    ids["s6"] = _state(conn, state="ACTIVE", reason="resumed: z", at=214)
    applied(ids["r3"], ids["s6"], 213)
    ids["s7"] = _state(conn, reason="by hand", at=218)
    ids["k6"] = _trading(conn, action="kill", reason="d", created=219)
    applied(ids["k6"], ids["s7"], 221)
    # A refused request has no effect.
    ids["rejected"] = _trading(conn, settle="state='rejected', processed_at_ms=220, outcome_reason='x'")
    # Capital: an applied enable wrote a revision; an unchanged one names the revision in force.
    ids["c1"] = _capital(conn, action="enable")
    ids["v1"] = _revision(conn, revision=2, source=f'{{"request_id": "{ids["c1"]}"}}')
    conn.exec_driver_sql(f"UPDATE capital_policy_requests SET state='applied', processed_at_ms=2, "
                         f"policy_revision_id='{ids['v1']}' WHERE request_id='{ids['c1']}'")
    ids["c2"] = _capital(conn, action="enable", settle=(
        f"state='applied', processed_at_ms=3, outcome_reason='unchanged', policy_revision_id='{ids['v1']}'"))
    ids["v2"] = _revision(conn, revision=3, source='{"amendment_digest": "owner"}')
    ids["u1"] = _resolved_uncertainty(conn, "state='applied', processed_at_ms=4")
    return ids


def test_the_upgrade_links_each_effect_to_the_request_that_caused_it(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        ids = _history(conn)
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    with engine.connect() as conn:
        states = dict(conn.execute(text("SELECT id, operator_request_id::text FROM trading_state")).all())
        assert {key: states[ids[key]] for key in ("s1", "s2", "s3", "s4", "s5a", "s5", "s6", "s7")} == {
            "s1": ids["k1"], "s2": ids["r1"], "s3": None, "s4": ids["k4"], "s5a": ids["r2"], "s5": None,
            "s6": ids["r3"], "s7": None}
        audits = dict(conn.execute(text(
            "SELECT attempt_id::text, min(operator_request_id::text) FROM funding_cancel_all_audit "
            "GROUP BY attempt_id")).all())
        assert {key: audits[ids[key]] for key in (
            "k1_ust", "k1_usd", "k4_ust", "k2_ust", "admin_ust", "k3_ust")} == {
            "k1_ust": ids["k1"], "k1_usd": ids["k1"], "k4_ust": ids["k4"], "k2_ust": ids["k2"],
            "admin_ust": None, "k3_ust": ids["k3"]}
        revisions = dict(conn.execute(text(
            "SELECT id::text, operator_request_id::text FROM capital_policy_revisions")).all())
        assert (revisions[ids["v1"]], revisions[ids["v2"]], revisions[_P]) == (ids["c1"], None, None)
        # Append-only again, for the owner too.
        assert conn.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgenabled <> 'O' AND tgname IN "
                                "('trading_state_history', 'immutable_capital_write', "
                                "'funding_cancel_all_audit_history')")) == 0
    _refused(engine, "UPDATE trading_state SET operator_request_id = NULL", "immutable trading state history")


def _other_scope_revision(conn) -> str:
    return _revision(conn, revision=1, symbol="fBTC")


def _plant(conn, sql: str) -> None:
    """The owner's way around the old rules: drop what would refuse the planted row."""
    conn.exec_driver_sql(sql)


# Each precondition, and a row that breaks it (planted by the owner on the revision before).
_VIOLATIONS = {
    "trading request still requested but linked to a trading state": lambda conn: (
        _plant(conn, "ALTER TABLE trading_control_requests DROP CONSTRAINT ck_trading_control_requests_outcome, "
                     "DISABLE TRIGGER trading_control_request_transition"),
        _trading(conn, settle=f"trading_state_id={_state(conn)}")),
    "capital request still requested but linked to a revision": lambda conn: (
        _plant(conn, "ALTER TABLE capital_policy_requests DROP CONSTRAINT ck_capital_policy_requests_outcome, "
                     "DISABLE TRIGGER capital_policy_request_transition"),
        _capital(conn, settle=f"policy_revision_id='{_P}'")),
    "capital request still requested but a revision names it": lambda conn: (
        _revision(conn, revision=2, source=f'{{"request_id": "{_capital(conn)}"}}')),
    "uncertainty request still requested but a journal row names it": lambda conn: (
        _resolved_uncertainty(conn)),
    "uncertainty request rejected or failed but a journal row names it": lambda conn: (
        _resolved_uncertainty(conn, "state='rejected', processed_at_ms=2, outcome_reason='x'")),
    "uncertainty request applied without a journal row": lambda conn: (
        _plant(conn, "ALTER TABLE uncertainty_resolution_requests "
                     "DISABLE TRIGGER uncertainty_resolution_request_transition"),
        _uncertainty(conn, settle="state='applied', processed_at_ms=2")),
    "settled request without processed_at_ms": lambda conn: (
        _plant(conn, "ALTER TABLE trading_control_requests DROP CONSTRAINT ck_trading_control_requests_outcome"),
        _trading(conn, settle="state='rejected', outcome_reason='x'")),
    "trading request linked to a trading state of another scope": lambda conn: (
        _trading(conn, settle=f"state='applied', processed_at_ms=2, trading_state_id={_state(conn, account=_OTHER)}")),
    "capital request linked to a revision of another scope": lambda conn: (
        _capital(conn, settle=(
            f"state='applied', processed_at_ms=2, policy_revision_id='{_other_scope_revision(conn)}'"))),
    "uncertainty request named by a journal row of another scope": lambda conn: (
        _resolved_uncertainty(conn, "state='applied', processed_at_ms=2"),
        _plant(conn, "ALTER TABLE execution_resolution_journal DISABLE TRIGGER ALL"),
        _plant(conn, f"UPDATE execution_resolution_journal SET exchange_account_id = '{_OTHER}' "
                     "WHERE operator_request_id IS NOT NULL")),
    "uncertainty request named by a journal row of another subject": lambda conn: (
        _uncertainty_resolving_another_quarantine(conn)),
    "cancel-all attempt of another scope than its trading state": lambda conn: (
        _audit(conn, state_id=_state(conn), actor="operator", currency="UST", at=1, account=_OTHER)),
    "revision source.request_id that is not a request id": lambda conn: (
        _revision(conn, revision=2, source='{"request_id": "nope"}')),
    "revision source.request_id naming no request of its scope and currency": lambda conn: (
        _revision(conn, revision=2, source=f'{{"request_id": "{uuid4()}"}}')),
    "request named by the source of more than one revision": lambda conn: (
        _two_revisions_of_one_request(conn)),
    "currency cancelled more than once inside one kill's window": lambda conn: (
        _kill_with_two_cancel_alls_of_one_currency(conn)),
}


def _uncertainty_resolving_another_quarantine(conn) -> None:
    """An applied request whose journal row resolves a quarantine it did not name."""
    request = _uncertainty(conn)
    _journal(conn, request)
    conn.exec_driver_sql("UPDATE uncertainty_resolution_requests SET state='applied', processed_at_ms=2 "
                         f"WHERE request_id='{request}'")


def _two_revisions_of_one_request(conn) -> None:
    request = _capital(conn, settle="state='rejected', processed_at_ms=2, outcome_reason='x'")
    for revision in (2, 3):
        _revision(conn, revision=revision, source=f'{{"request_id": "{request}"}}')


def _kill_with_two_cancel_alls_of_one_currency(conn) -> None:
    kill = _trading(conn, action="kill", reason="a", created=1)
    state_id = _state(conn, reason="kill: a", at=2)
    conn.exec_driver_sql(f"UPDATE trading_control_requests SET state='applied', processed_at_ms=3, "
                         f"trading_state_id={state_id} WHERE request_id='{kill}'")
    for at in (4, 6):
        _audit(conn, state_id=state_id, actor="operator", currency="UST", at=at)


def test_every_precondition_has_a_violation_case() -> None:
    from .test_database_realm import _load

    names = [what for what, _sql in _load(f"{_REVISION}_operator_request_causation.py").PRECONDITIONS]
    assert sorted(names) == sorted(_VIOLATIONS)


@pytest.mark.parametrize("precondition", sorted(_VIOLATIONS))
def test_each_precondition_refuses_the_upgrade_and_names_the_count(before, precondition) -> None:
    url, engine = before
    with engine.begin() as conn:
        _VIOLATIONS[precondition](conn)
    with pytest.raises(RuntimeError, match=f"{precondition}: 1"):
        alembic(url, "upgrade", "head")
    with engine.connect() as conn:  # nothing changed
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == LAST_REVERSIBLE_REVISION
        assert conn.scalar(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'trading_state' AND column_name = 'operator_request_id'")) == 0
