"""Bounded ledger digest of a restored cluster compared with production (host side, stdlib only).

The capital authority is the ledger (journals + venue observations). A restore is accepted when
every append-only ledger row the restored copy holds is byte-identical on production and
production holds no other row "before" the restored copy's boundary. Both clusters are read
the same way: the admin role through `docker exec ... psql` on the container's own socket, one
REPEATABLE READ READ ONLY transaction, `COPY (SELECT '<table>', t.* ...) TO STDOUT`. The same
generated script runs on both, so the two sides need no shared canonicalizer: equal stored
values print equal COPY text (same PostgreSQL build, pinned image labels).

Boundary (W). Rows carry no common commit stamp, so each table is bounded by a key that is
commit-ordered per scope (every ledger writer of a scope holds the scope's transaction advisory
lock until commit, and the tables are append-only by trigger). W is taken from the RESTORED
copy -- its newest committed state -- and production is read at that W:

* ledger_observation_query: ``query_revision`` (max + 1 under the lock) <= the restored max.
* ledger_observation, its six child tables, accepted_capital_basis and its six child tables,
  quarantine_member: rows written in the transaction that accepts one observation, bounded by
  that observation's ``query_revision`` <= the restored max over observations. Commit order
  equals query order for accepted observations (trigger: only the scope's latest query can be
  accepted) and, for fenced ones, because one cycle per scope runs begin -> accept in turn; a
  violation shows as a mismatch (fail-closed), never as a silent pass.
* execution_resolution_journal: may be written after its observation (an operator), but only
  while that observation is the scope's latest accepted one (record_resolution). Rows that name
  an observation older than the restored latest accepted one are complete; rows naming that
  latest one are not compared (strict ``<``).
* submission_attempt_journal: ``attempt_seq`` (max + 1 under the lock) <= the restored max.
* transport_outcome_journal: one row per attempt, written later: attempts <= the restored max
  that HAVE an outcome in the restored copy (the restored attempts without one are excluded).
* quarantine_opening: ``opened_revision`` (the bumped clock) <= the restored max.
* capital_authority_epoch: global, ``epoch_seq`` <= the restored max.
* Mutable tables (the clock, the mirrors) are never compared with production: the restored
  clock must not be ahead of production's, must cover its own openings and accepts, and every
  restored mirror row must name an accepted observation of its scope.

The restored copy's bounded set must equal its whole table (resolutions: at most), so a bound
rule that drops restored rows fails instead of hiding them.

Digest per table: count plus the sum of sha256(domain + row line) modulo 2**256 (a multiset
hash: no ORDER BY on production, O(1) memory while streaming).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

DIGEST_VERSION = 1
_DOMAIN = b"bfx-ledger-restore-digest/v1\n"
_MODULUS = 1 << 256
_ENVIRONMENT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MAX_PENDING = 10_000


class LedgerVerificationError(ValueError):
    """A bounded failure code of the ledger comparison; ``detail`` names tables, never data."""

    def __init__(self, code: str, detail: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.detail = detail


def _fail(code: str, detail: tuple[str, ...] = ()) -> None:
    raise LedgerVerificationError(code, detail)


@dataclass(frozen=True, slots=True)
class Rule:
    join: str  # FROM public.<table> t <join>
    where: str  # predicate over t, the joins and the bounds row b (or {epoch}/{pending})
    bound: str  # the restored maximum the rule is bounded by (evidence)


_SCOPE = "JOIN b ON ({a}.exchange_account_id, {a}.deployment_environment) = (b.account, b.env)"
_QUERY_OF_OBS = "JOIN public.ledger_observation_query q ON q.query_id = o.query_id "
_VIA_OBSERVATION = (
    "JOIN public.ledger_observation o ON o.id = t.observation_id " + _QUERY_OF_OBS
    + _SCOPE.format(a="o")
)
_VIA_BASIS = (
    "JOIN public.accepted_capital_basis ab ON ab.id = t.basis_id "
    "JOIN public.ledger_observation o ON o.id = ab.observation_id " + _QUERY_OF_OBS
    + _SCOPE.format(a="o")
)
_OBSERVED = Rule(_VIA_OBSERVATION, "q.query_revision <= b.q_obs", "observation_query_revision")
_IN_BASIS = Rule(_VIA_BASIS, "q.query_revision <= b.q_obs", "observation_query_revision")

# Append-only tables compared with production, in a fixed order.
RULES: dict[str, Rule] = {
    "ledger_observation_query": Rule(
        _SCOPE.format(a="t"), "t.query_revision <= b.q_query", "query_revision"),
    "ledger_observation": Rule(
        "JOIN public.ledger_observation_query q ON q.query_id = t.query_id "
        + _SCOPE.format(a="t"),
        "q.query_revision <= b.q_obs", "observation_query_revision"),
    "ledger_observation_wallet": _OBSERVED,
    "ledger_observation_offer": _OBSERVED,
    "ledger_observation_credit": _OBSERVED,
    "ledger_observation_offer_history": _OBSERVED,
    "ledger_observation_credit_history": _OBSERVED,
    "ledger_observation_trade": _OBSERVED,
    "submission_attempt_journal": Rule(
        _SCOPE.format(a="t"), "t.attempt_seq <= b.a_max", "attempt_seq"),
    "transport_outcome_journal": Rule(
        "JOIN public.submission_attempt_journal a ON a.attempt_id = t.attempt_id "
        + _SCOPE.format(a="a"),
        "a.attempt_seq <= b.a_max AND t.attempt_id <> ALL({pending})", "attempt_seq"),
    "quarantine_opening": Rule(
        _SCOPE.format(a="t"), "t.opened_revision <= b.r_open", "opened_revision"),
    "quarantine_member": _OBSERVED,
    "execution_resolution_journal": Rule(
        _VIA_OBSERVATION, "q.query_revision < b.q_acc", "accepted_observation_query_revision"),
    "accepted_capital_basis": _OBSERVED,
    "accepted_capital_basis_symbol": _IN_BASIS,
    "accepted_capital_basis_cell": _IN_BASIS,
    "accepted_capital_basis_credit": _IN_BASIS,
    "accepted_capital_basis_credit_cell": _IN_BASIS,
    "accepted_capital_basis_attempt": _IN_BASIS,
    "accepted_capital_basis_quarantine": _IN_BASIS,
    "capital_authority_epoch": Rule("", "t.epoch_seq <= {epoch}", "epoch_seq"),
}
# Tables whose restored bounded set may be smaller than the restored table (rows naming the
# restored latest accepted observation are not compared).
PARTIAL_TABLES = frozenset({"execution_resolution_journal"})
# Updated in place: checked on the restored copy only.
MUTABLE_TABLES = ("capital_command_clock", "venue_offer_mirror", "venue_credit_mirror")
# Every table the bounds script itself reads; they predate the switch, so a missing one is a
# schema the drill cannot verify.
_CORE_TABLES = frozenset({
    "capital_command_clock", "ledger_observation_query", "ledger_observation",
    "submission_attempt_journal", "transport_outcome_journal", "quarantine_opening",
    "capital_authority_epoch", "venue_offer_mirror", "venue_credit_mirror",
})
LEDGER_TABLES = (*RULES, *MUTABLE_TABLES)
# What the image's boot check (``apps/restore_boot_check.py``) reads besides the ledger: the
# schema head, the realm stamp and the capital policy the capital reader folds. The drill's
# per-run verifier role gets SELECT on exactly these and the ledger tables, nothing else.
BOOT_CHECK_EXTRA_TABLES = (
    "alembic_version", "database_realm", "capital_policy_heads", "capital_policy_revisions",
)
VERIFIER_TABLES = (*LEDGER_TABLES, *BOOT_CHECK_EXTRA_TABLES)
_SCOPE_OWNERS = (
    "capital_command_clock", "ledger_observation_query", "submission_attempt_journal",
    "quarantine_opening",
)


@dataclass(frozen=True, slots=True)
class ScopeBounds:
    account: str
    environment: str
    q_query: int | None
    q_obs: int | None
    q_acc: int | None
    a_max: int | None
    r_open: int | None
    clock: int | None

    def evidence(self) -> dict[str, object]:
        return {
            "exchange_account_id": self.account, "deployment_environment": self.environment,
            "query_revision": self.q_query, "observation_query_revision": self.q_obs,
            "accepted_observation_query_revision": self.q_acc, "attempt_seq": self.a_max,
            "opened_revision": self.r_open, "clock_revision": self.clock,
        }


@dataclass(frozen=True, slots=True)
class Bounds:
    server_version_num: int
    migration_heads: tuple[str, ...]
    present: frozenset[str]
    scopes: tuple[ScopeBounds, ...]
    pending: tuple[str, ...]
    epoch: int | None
    authority: str | None
    inconsistencies: Mapping[str, int]


def bounds_script() -> str:
    """Restored copy only: schema, present tables, per-scope W, pending attempts, self-checks."""
    names = ", ".join(f"'{name}'" for name in LEDGER_TABLES)
    scopes = " UNION ".join(
        f"SELECT exchange_account_id, deployment_environment FROM public.{owner}"
        for owner in _SCOPE_OWNERS
    )
    in_scope = "(x.exchange_account_id, x.deployment_environment) = (s.account, s.env)"
    return (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        "SET LOCAL lock_timeout = '10s';\n"
        "SELECT 'schema', current_setting('server_version_num'), "
        "(SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version);\n"
        "SELECT 'table', c.relname FROM pg_catalog.pg_class c "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') "
        f"AND c.relname = ANY(ARRAY[{names}]) ORDER BY 2;\n"
        f"WITH s(account, env) AS ({scopes}), w AS (SELECT s.account, s.env, "
        f"(SELECT max(x.query_revision) FROM public.ledger_observation_query x WHERE {in_scope}) "
        "AS q_query, "
        "(SELECT max(q.query_revision) FROM public.ledger_observation x JOIN "
        f"public.ledger_observation_query q ON q.query_id = x.query_id WHERE {in_scope}) AS q_obs, "
        "(SELECT max(q.query_revision) FROM public.ledger_observation x JOIN "
        "public.ledger_observation_query q ON q.query_id = x.query_id "
        f"WHERE {in_scope} AND x.accepted) AS q_acc, "
        f"(SELECT max(x.attempt_seq) FROM public.submission_attempt_journal x WHERE {in_scope}) "
        "AS a_max, "
        f"(SELECT max(x.opened_revision) FROM public.quarantine_opening x WHERE {in_scope}) "
        "AS r_open, "
        f"(SELECT x.revision FROM public.capital_command_clock x WHERE {in_scope}) AS clock "
        "FROM s) "
        "SELECT 'scope', account, env, q_query, q_obs, q_acc, a_max, r_open, clock FROM w "
        "ORDER BY 2, 3;\n"
        "SELECT 'pending', a.attempt_id FROM public.submission_attempt_journal a "
        "WHERE NOT EXISTS (SELECT 1 FROM public.transport_outcome_journal t "
        "WHERE t.attempt_id = a.attempt_id) ORDER BY 2;\n"
        "SELECT 'epoch', (SELECT max(epoch_seq) FROM public.capital_authority_epoch), "
        "(SELECT authority FROM public.capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1);\n"
        # The clock is bumped by every opening and read by every accept: never behind either.
        f"WITH s(account, env) AS ({scopes}) SELECT 'inconsistent', 'clock_behind', count(*) "
        "FROM s LEFT JOIN public.capital_command_clock c "
        "ON (c.exchange_account_id, c.deployment_environment) = (s.account, s.env) "
        "CROSS JOIN LATERAL (SELECT "
        f"(SELECT max(x.opened_revision) FROM public.quarantine_opening x WHERE {in_scope}) AS o, "
        f"(SELECT max(x.accept_revision) FROM public.ledger_observation x WHERE {in_scope} "
        "AND x.accepted) AS a) m "
        "WHERE (m.o IS NOT NULL OR m.a IS NOT NULL) "
        "AND coalesce(c.revision, -1) < greatest(coalesce(m.o, 0), coalesce(m.a, 0));\n"
        + "".join(
            f"SELECT 'inconsistent', '{name}_orphans', count(*) FROM public.{name} m "
            "WHERE NOT EXISTS (SELECT 1 FROM public.ledger_observation o "
            "WHERE o.id = m.last_accepted_observation_id AND o.accepted "
            "AND (o.exchange_account_id, o.deployment_environment) "
            "= (m.exchange_account_id, m.deployment_environment));\n"
            for name in ("venue_offer_mirror", "venue_credit_mirror")
        )
        + "ROLLBACK;\n"
    )


def _optional_int(text: str) -> int | None:
    if text == "":
        return None
    if not text.isdigit() or len(text) > 19:
        _fail("ledger_bounds_invalid")
    return int(text)


def _canonical_uuid(text: str) -> str:
    try:
        value = str(UUID(text))
    except ValueError:
        _fail("ledger_bounds_invalid")
    if value != text:
        _fail("ledger_bounds_invalid")
    return value


def parse_bounds(output: str) -> Bounds:
    """Strictly parse the bounds script's psql -At output (tab separated)."""
    if not isinstance(output, str) or len(output) > 4_000_000:
        _fail("ledger_bounds_invalid")
    schema: tuple[int, tuple[str, ...]] | None = None
    present: set[str] = set()
    scopes: list[ScopeBounds] = []
    pending: list[str] = []
    epoch: tuple[int | None, str | None] | None = None
    inconsistencies: dict[str, int] = {}
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        kind = fields[0]
        if kind == "schema" and len(fields) == 3 and schema is None:
            version = _optional_int(fields[1])
            heads = tuple(fields[2].split(","))
            if not version or any(_IDENTIFIER.fullmatch(head) is None for head in heads):
                _fail("ledger_bounds_invalid")
            schema = (version, heads)
        elif kind == "table" and len(fields) == 2 and fields[1] in LEDGER_TABLES:
            present.add(fields[1])
        elif kind == "scope" and len(fields) == 9:
            if _ENVIRONMENT.fullmatch(fields[2]) is None:
                _fail("ledger_bounds_invalid")
            scopes.append(ScopeBounds(
                _canonical_uuid(fields[1]), fields[2],
                *(_optional_int(value) for value in fields[3:]),  # type: ignore[arg-type]
            ))
        elif kind == "pending" and len(fields) == 2:
            pending.append(_canonical_uuid(fields[1]))
        elif kind == "epoch" and len(fields) == 3 and epoch is None:
            epoch = (_optional_int(fields[1]), fields[2] or None)
        elif (kind == "inconsistent" and len(fields) == 3 and fields[1] not in inconsistencies
              and re.fullmatch(r"[a-z_]{1,64}", fields[1])):
            count = _optional_int(fields[2])
            if count is None:
                _fail("ledger_bounds_invalid")
            inconsistencies[fields[1]] = count
        else:
            _fail("ledger_bounds_invalid")
    keys = [(scope.account, scope.environment) for scope in scopes]
    if (schema is None or epoch is None or len(keys) != len(set(keys))
            or len(pending) != len(set(pending)) or len(pending) > _MAX_PENDING
            or set(inconsistencies) != {"clock_behind", "venue_offer_mirror_orphans",
                                        "venue_credit_mirror_orphans"}):
        _fail("ledger_bounds_invalid")
    if not _CORE_TABLES.issubset(present):
        _fail("ledger_schema_incomplete")
    return Bounds(schema[0], schema[1], frozenset(present), tuple(scopes), tuple(pending),
                  epoch[0], epoch[1], inconsistencies)


def _literal(value: int | None) -> str:
    return "NULL::bigint" if value is None else f"{int(value)}::bigint"


def _bounds_cte(bounds: Bounds) -> str:
    rows = ", ".join(
        f"('{scope.account}'::uuid, '{scope.environment}'::text, {_literal(scope.q_query)}, "
        f"{_literal(scope.q_obs)}, {_literal(scope.q_acc)}, {_literal(scope.a_max)}, "
        f"{_literal(scope.r_open)})"
        for scope in bounds.scopes
    )
    return f"WITH b(account, env, q_query, q_obs, q_acc, a_max, r_open) AS (VALUES {rows}) "


def compared_tables(bounds: Bounds) -> tuple[str, ...]:
    """The rule tables present in the restored schema (a newer release may know more)."""
    return tuple(name for name in RULES if name in bounds.present)


IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000

# Why a cluster read failed, as a bounded code for the receipt and the journal (never psql's
# raw text: an error's DETAIL can quote row values). Matched on PostgreSQL's message text.
_READ_FAILURE_MESSAGES = (
    ("canceling statement due to statement timeout", "statement_timeout"),
    ("terminating connection due to transaction timeout", "transaction_timeout"),
    ("canceling statement due to lock timeout", "lock_timeout"),
    ("terminating connection due to idle-in-transaction timeout", "idle_in_transaction_timeout"),
    ("permission denied", "permission"),
    ("could not connect", "connection"),
    ("server closed the connection", "connection"),
)
READ_FAILURE_CAUSES = frozenset(
    {cause for _, cause in _READ_FAILURE_MESSAGES} | {"client_deadline", "other"}
)


def classify_read_failure(status: int, stderr: str) -> str:
    """A killed reader (negative status: the drill's own deadline) or the first known message."""
    if status < 0:
        return "client_deadline"
    for message, cause in _READ_FAILURE_MESSAGES:
        if message in stderr:
            return cause
    return "other"



def digest_script(bounds: Bounds, *, restored: bool, timeout_ms: int) -> str:
    """The bounded COPY script; identical on both clusters except the restored-only totals.

    ``timeout_ms`` (the drill's remaining budget) bounds the whole snapshot on the server
    (``transaction_timeout``, PostgreSQL 17+) and every statement, so a reader whose client was
    killed cannot keep production's snapshot and AccessShareLocks past the drill: a migration
    queued behind them waits at most that long. An abandoned session between statements ends
    after ``IDLE_IN_TRANSACTION_TIMEOUT_MS``.
    """
    if not bounds.scopes:
        _fail("ledger_empty")
    if type(timeout_ms) is not int or timeout_ms <= 0:
        _fail("restore_output_invalid")
    cte = _bounds_cte(bounds)
    pending = (
        "ARRAY[" + ", ".join(f"'{value}'" for value in bounds.pending) + "]::uuid[]"
        if bounds.pending else "'{}'::uuid[]"
    )
    lines = [
        # Pin every setting that shapes COPY text (pg_dump does the same), whatever the
        # role's or server's defaults on either cluster.
        "SET client_encoding = 'UTF8';",
        "SET DateStyle = 'ISO, YMD';",
        "SET IntervalStyle = 'postgres';",
        "SET TimeZone = 'UTC';",
        "SET extra_float_digits = 3;",
        f"SET statement_timeout = {timeout_ms};",
        f"SET transaction_timeout = {timeout_ms};",
        f"SET idle_in_transaction_session_timeout = {IDLE_IN_TRANSACTION_TIMEOUT_MS};",
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;",
        "SET LOCAL lock_timeout = '10s';",
    ]
    for name in compared_tables(bounds):
        rule = RULES[name]
        where = rule.where.format(pending=pending, epoch=_literal(bounds.epoch))
        lines.append(
            f"COPY ({cte}SELECT '{name}'::text, t.* FROM public.{name} t {rule.join} "
            f"WHERE {where}) TO STDOUT;"
        )
    lines.append(
        f"COPY ({cte}SELECT '#clock'::text, t.exchange_account_id, t.deployment_environment, "
        f"t.revision FROM public.capital_command_clock t {_SCOPE.format(a='t')}) TO STDOUT;"
    )
    if restored:
        lines.extend(
            f"COPY (SELECT '#count'::text, '{name}'::text, count(*) FROM public.{name}) TO STDOUT;"
            for name in LEDGER_TABLES if name in bounds.present
        )
    lines.append("ROLLBACK;")
    return "\n".join(lines) + "\n"


@dataclass(slots=True)
class StreamDigest:
    """Consumes COPY lines (bytes) and keeps a count and a multiset hash per table."""

    tables: tuple[str, ...]
    counts: dict[str, int] = field(default_factory=dict)
    sums: dict[str, int] = field(default_factory=dict)
    clocks: dict[tuple[str, str], int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    invalid: bool = False

    def __post_init__(self) -> None:
        self.counts = dict.fromkeys(self.tables, 0)
        self.sums = dict.fromkeys(self.tables, 0)

    def __call__(self, line: bytes) -> None:
        if line.endswith(b"\n"):
            line = line[:-1]
        name, _, rest = line.partition(b"\t")
        try:
            table = name.decode("ascii")
        except UnicodeDecodeError:
            self.invalid = True
            return
        if table in self.counts:
            self.counts[table] += 1
            self.sums[table] = (
                self.sums[table] + int.from_bytes(hashlib.sha256(_DOMAIN + line).digest(), "big")
            ) % _MODULUS
            return
        fields = rest.decode("utf-8", errors="replace").split("\t")
        if table == "#clock" and len(fields) == 3 and fields[2].isdigit():
            key = (fields[0], fields[1])
            if key in self.clocks:
                self.invalid = True
            self.clocks[key] = int(fields[2])
        elif table == "#count" and len(fields) == 2 and fields[1].isdigit() \
                and fields[0] not in self.totals:
            self.totals[fields[0]] = int(fields[1])
        else:
            self.invalid = True

    def digests(self) -> dict[str, dict[str, object]]:
        return {
            name: {"count": self.counts[name], "digest": format(self.sums[name], "064x")}
            for name in self.tables
        }


def compare(bounds: Bounds, restored: StreamDigest, production: StreamDigest) -> dict[str, object]:
    """The ledger part of the receipt, or LedgerVerificationError (bounded code, table names)."""
    if restored.invalid:
        _fail("restore_output_invalid")
    if production.invalid:
        _fail("production_read_failed")
    inconsistent = tuple(sorted(name for name, count in bounds.inconsistencies.items() if count))
    if inconsistent:
        _fail("ledger_restored_inconsistent", inconsistent)
    if bounds.authority != "ledger":
        _fail("ledger_authority_not_ledger")
    tables = compared_tables(bounds)
    if set(restored.totals) != {name for name in LEDGER_TABLES if name in bounds.present}:
        _fail("restore_output_invalid")
    unbounded = tuple(
        name for name in tables
        if (restored.counts[name] > restored.totals[name] if name in PARTIAL_TABLES
            else restored.counts[name] != restored.totals[name])
    )
    if unbounded:
        _fail("ledger_bound_invalid", unbounded)
    scope_keys = {(scope.account, scope.environment) for scope in bounds.scopes}
    expected_clocks = {
        (scope.account, scope.environment): scope.clock
        for scope in bounds.scopes if scope.clock is not None
    }
    if restored.clocks != expected_clocks or not set(production.clocks) <= scope_keys:
        _fail("restore_output_invalid")
    if any(production.clocks.get(key, -1) < revision for key, revision in expected_clocks.items()):
        _fail("ledger_ahead_of_production")
    restored_digests, production_digests = restored.digests(), production.digests()
    mismatched = tuple(
        name for name in tables if restored_digests[name] != production_digests[name]
    )
    if mismatched:
        _fail("ledger_digest_mismatch", mismatched)
    return {
        "digest_version": DIGEST_VERSION,
        "scopes": [
            {**scope.evidence(),
             "production_clock_revision": production.clocks.get((scope.account, scope.environment))}
            for scope in bounds.scopes
        ],
        "pending_attempts": len(bounds.pending),
        "epoch_seq": bounds.epoch,
        "tables": {
            name: {**restored_digests[name], "bound": RULES[name].bound,
                   "restored_total": restored.totals[name]}
            for name in tables
        },
        "mutable": {name: {"restored_total": restored.totals[name]}
                    for name in MUTABLE_TABLES if name in restored.totals},
        "absent": sorted(set(RULES) - set(tables)),
        "rows_compared": sum(restored.counts[name] for name in tables),
    }


BOOT_ERROR_CODES = frozenset({
    "boot_schema_head_mismatch", "boot_realm_mismatch", "boot_authority_not_ledger",
    "boot_ledger_empty", "boot_seed_missing", "boot_basis_missing", "boot_capital_read_failed",
    "boot_capital_unread", "boot_check_failed",
})


# Every code this module raises, plus the image check's refusals.
ERROR_CODES = BOOT_ERROR_CODES | frozenset({
    "ledger_bounds_invalid", "ledger_schema_incomplete", "ledger_empty",
    "ledger_restored_inconsistent", "ledger_authority_not_ledger", "ledger_bound_invalid",
    "ledger_ahead_of_production", "ledger_digest_mismatch", "restore_output_invalid",
    "production_read_failed",
})


def boot_failure_code(stdout: str) -> str:
    """The image check's bounded refusal code from its last JSON line, else a generic one."""
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    try:
        code = json.loads(lines[-1]).get("error") if lines else None
    except (ValueError, AttributeError):
        code = None
    return code if code in BOOT_ERROR_CODES else "boot_check_failed"


# The boot check's output contract. The JSON comes from the DEPLOYED image and is parsed by the
# TARGET release's drill, so each level is validated by its required keys and their types and
# unknown keys are ignored (a newer image may report more); the receipt keeps the known keys.
_BOOT_KEYS = {"schema_head": str, "realm": str, "authority": str, "scopes": list}
_BOOT_SCOPE_KEYS = {"exchange_account_id": str, "deployment_environment": str, "basis_id": str,
                    "reads": list}
_BOOT_READ_KEYS = {"symbol": str, "cell_id": str, "basis_id": (str, type(None)), "result": str}


def _known(value: object, keys: Mapping[str, type | tuple[type, ...]]) -> dict[str, Any]:
    """``value``'s required keys, each of its type; anything else in it is ignored."""
    if not isinstance(value, dict) or any(
            key not in value or not isinstance(value[key], kind) for key, kind in keys.items()):
        _fail("restore_output_invalid")
    return {key: value[key] for key in keys}


def parse_boot(output: str, bounds: Bounds) -> dict[str, object]:
    """Validate the image boot check's single JSON line against the restored bounds."""
    lines = [line for line in output.splitlines() if line.strip()] if isinstance(output, str) else []
    if len(lines) != 1 or len(lines[0]) > 65_536:
        _fail("restore_output_invalid")
    try:
        payload = json.loads(lines[0])
    except ValueError:
        _fail("restore_output_invalid")
    boot = _known(payload.get("boot") if isinstance(payload, dict) else None, _BOOT_KEYS)
    if boot["authority"] != "ledger" or _ENVIRONMENT.fullmatch(boot["realm"]) is None:
        _fail("restore_output_invalid")
    if (boot["schema_head"],) != tuple(bounds.migration_heads):
        _fail("boot_schema_head_mismatch")
    seen: list[tuple[str, str]] = []
    scopes: list[dict[str, Any]] = []
    for raw_scope in boot["scopes"]:
        scope = _known(raw_scope, _BOOT_SCOPE_KEYS)
        seen.append((scope["exchange_account_id"], scope["deployment_environment"]))
        _canonical_uuid(scope["basis_id"])
        reads = [_known(read, _BOOT_READ_KEYS) for read in scope["reads"]]
        for read in reads:
            if (not (read["result"] == "available" or read["result"].startswith("blocked:"))
                    or (read["basis_id"] is None
                        and read["result"] != "blocked:snapshot_query_pending")):
                _fail("restore_output_invalid")
        scopes.append({**scope, "reads": reads})
    expected = sorted((scope.account, scope.environment) for scope in bounds.scopes)
    if (len(seen) != len(set(seen)) or sorted(seen) != expected
            or not any(scope["reads"] for scope in scopes)):
        _fail("restore_output_invalid")
    return {**boot, "scopes": scopes}


def validate_tables(names: Iterable[str]) -> None:
    """Every name a safe SQL identifier (the rules are static; this guards edits)."""
    if any(_TABLE_NAME.fullmatch(name) is None for name in names):
        raise ValueError("unsafe table name")


validate_tables(LEDGER_TABLES)
