"""Retire Better Auth: drop the ``auth`` schema, the ``bfx_webauth`` role and the old memberships.

Quality plan 9d-7 (security/9d-tailscale-ingress-brief.md §4, the contract step). From 9d-1
(``e489261c10b1``) operator authority is membership alone and nothing in the database reads
``auth``; from 9d-5 the operator reaches the web API through Tailscale only, so nothing
connects as ``bfx_webauth`` either. The operator's memberships are keyed by the tailnet user
ID (digits, W3); the rows keyed by a Better Auth user ID are the leftovers this removes.

In one transaction, under SHARE locks on the four operator outboxes:

A. every ``active``/``halted`` account keeps an owner/operator membership keyed by a numeric
   ID; otherwise refuse (deleting the old rows would leave the account with no operator, and
   kill and the CLI would stop working for it);
B. no outbox holds a ``requested`` row from a non-numeric ``requested_by``: the daemon would
   reject it once the old memberships are gone, and a kill must not be refused silently;
C. the non-numeric memberships are deleted, exactly as many as were counted first;
D. every default privilege in ``auth`` is revoked, so the next checks see only real objects;
E. before ``DROP SCHEMA auth CASCADE``: the only foreign key from outside ``auth`` into it is
   ``public.user_profiles.fk_user_profiles_user``; no function outside ``auth`` names ``auth.``
   in its body; and in ``pg_depend`` nothing outside ``auth`` depends on the schema or an
   object in it (relations, constraints, functions, view rules, triggers, column defaults,
   types and policies), except that foreign key. CASCADE would otherwise drop a view, a
   trigger, a column default or a function outside ``auth`` without a word;
F. ``user_profiles``, ``user_configs`` and ``api_keys`` keep their rows: they are not rekeyed
   (the vault's AAD binds ``user_id``) and only lose the foreign key to ``auth."user"``;
G. ``bfx_webauth`` is dropped as ``d3e4f5a6b7c8`` drops ``bfx_cutover_reader``: refused if it
   owns anything or holds anything but relation, schema and default privileges here;
   whatever it still holds after the schema is gone is revoked, and ``pg_shdepend`` must be
   empty for this database; the role stays, holding nothing here, with a NOTICE when another
   database of the cluster still references it (the test clusters; production has one).
   A database without the role skips this step.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md): the Better Auth tables and the
role are not recreated.

Revision ID: f836fdee9bf3
Revises: 89886d27750b
"""
from sqlalchemy import text

from alembic import op

revision = "f836fdee9bf3"
down_revision = "89886d27750b"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

WEBAUTH = "bfx_webauth"
AUTH = "auth"
OUTBOXES = ("trading_control_requests", "capital_policy_requests",
            "uncertainty_resolution_requests", "credential_rotation_requests")
# Keyed by the old user ID and not rekeyed (AAD): their row counts must not change.
LEGACY = ("user_profiles", "user_configs", "api_keys")
NUMERIC = "'^[0-9]+$'"
KEPT_FK = ("public", "user_profiles", "fk_user_profiles_user")

_ACCOUNTS_WITHOUT_OPERATOR = f"""
SELECT count(*) FROM public.exchange_accounts a
WHERE a.lifecycle_status IN ('active', 'halted')
  AND NOT EXISTS (SELECT 1 FROM public.exchange_account_memberships m
                  WHERE m.exchange_account_id = a.id AND m.role IN ('owner', 'operator')
                    AND m.user_id ~ {NUMERIC})
"""

# Default privileges defined in ``auth``, one row per (grantor, grantee, object type).
_AUTH_DEFAULTS = f"""
SELECT quote_ident(pg_get_userbyid(d.defaclrole)),
       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
       d.defaclobjtype
FROM pg_default_acl d, aclexplode(d.defaclacl) a
WHERE d.defaclnamespace = (SELECT oid FROM pg_namespace WHERE nspname = '{AUTH}')
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""
_DEFAULT_OBJECTS = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES",
                    "n": "SCHEMAS", "L": "LARGE OBJECTS"}

# Foreign keys from a table outside ``auth`` to a table in it.
_FOREIGN_KEYS_INTO_AUTH = f"""
SELECT sn.nspname, s.relname, c.conname FROM pg_constraint c
JOIN pg_class s ON s.oid = c.conrelid JOIN pg_namespace sn ON sn.oid = s.relnamespace
JOIN pg_class r ON r.oid = c.confrelid JOIN pg_namespace rn ON rn.oid = r.relnamespace
WHERE c.contype = 'f' AND rn.nspname = '{AUTH}' AND sn.nspname <> '{AUTH}'
ORDER BY 1, 2, 3
"""

# Any spelling: auth."user", "auth"."user", auth.user, auth . "user", another auth table
# (the 9d-1 check, e489261c10b1).
_FUNCTIONS_READING_AUTH = rf"""
SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname <> '{AUTH}' AND p.prosrc ~* '(\m|")auth"?\s*\.'
"""

# The namespace of a catalog object, for the catalogs a schema's objects live in. A
# constraint, view rule, trigger, column default or policy belongs to its table's namespace,
# and a TOAST table or its index (in ``pg_toast``) to the namespace of the table it stores for.
_NAMESPACE_OF = """
CASE {cls}
  WHEN 'pg_namespace'::regclass THEN {obj}
  WHEN 'pg_class'::regclass THEN (
    SELECT coalesce((SELECT o.relnamespace FROM pg_class o
                     WHERE o.reltoastrelid IN (c.oid, (SELECT indrelid FROM pg_index
                                                       WHERE indexrelid = c.oid))),
                    c.relnamespace) FROM pg_class c WHERE c.oid = {obj})
  WHEN 'pg_proc'::regclass THEN (SELECT pronamespace FROM pg_proc WHERE oid = {obj})
  WHEN 'pg_type'::regclass THEN (SELECT typnamespace FROM pg_type WHERE oid = {obj})
  WHEN 'pg_constraint'::regclass THEN (SELECT connamespace FROM pg_constraint WHERE oid = {obj})
  WHEN 'pg_rewrite'::regclass THEN (SELECT c.relnamespace FROM pg_rewrite x
                                    JOIN pg_class c ON c.oid = x.ev_class WHERE x.oid = {obj})
  WHEN 'pg_trigger'::regclass THEN (SELECT c.relnamespace FROM pg_trigger x
                                    JOIN pg_class c ON c.oid = x.tgrelid WHERE x.oid = {obj})
  WHEN 'pg_attrdef'::regclass THEN (SELECT c.relnamespace FROM pg_attrdef x
                                    JOIN pg_class c ON c.oid = x.adrelid WHERE x.oid = {obj})
  WHEN 'pg_policy'::regclass THEN (SELECT c.relnamespace FROM pg_policy x
                                   JOIN pg_class c ON c.oid = x.polrelid WHERE x.oid = {obj})
  ELSE (SELECT oid FROM pg_namespace
        WHERE nspname = (pg_identify_object({cls}, {obj}, 0)).schema)
END
"""
# Objects outside ``auth`` that depend on the schema or on an object in it: what CASCADE
# would drop or alter outside the schema. Default privileges (pg_default_acl) are revoked
# first and do not count; neither do comments or other catalogs CASCADE only tidies.
_DEPENDENTS_OUTSIDE_AUTH = f"""
WITH auth_ns AS (SELECT oid FROM pg_namespace WHERE nspname = '{AUTH}')
SELECT DISTINCT pg_describe_object(d.classid, d.objid, 0)
FROM pg_depend d, auth_ns
WHERE d.classid IN ('pg_class'::regclass, 'pg_constraint'::regclass, 'pg_proc'::regclass,
                    'pg_rewrite'::regclass, 'pg_trigger'::regclass, 'pg_attrdef'::regclass,
                    'pg_type'::regclass, 'pg_policy'::regclass)
  AND {_NAMESPACE_OF.format(cls="d.refclassid", obj="d.refobjid")} = auth_ns.oid
  AND {_NAMESPACE_OF.format(cls="d.classid", obj="d.objid")} IS DISTINCT FROM auth_ns.oid
  AND NOT (d.classid = 'pg_constraint'::regclass AND d.objid = (
        SELECT oid FROM pg_constraint WHERE conname = '{KEPT_FK[2]}'
          AND conrelid = '{KEPT_FK[0]}.{KEPT_FK[1]}'::regclass))
ORDER BY 1
"""

# What ``bfx_webauth`` holds in this database besides relation, schema and default
# privileges (ownership, policies, other ACLs) and this database's own ACL: refused, a
# migration never gave it any of that.
_WEBAUTH_OTHER = f"""
WITH r AS (SELECT oid FROM pg_roles WHERE rolname = '{WEBAUTH}'),
here AS (SELECT oid FROM pg_database WHERE datname = current_database())
SELECT d.classid::regclass::text, d.deptype::text FROM pg_shdepend d, r, here
WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = r.oid
  AND ((d.dbid = here.oid AND (d.deptype <> 'a' OR d.classid NOT IN
        ('pg_class'::regclass, 'pg_namespace'::regclass, 'pg_default_acl'::regclass)))
       OR (d.dbid = 0 AND d.classid = 'pg_database'::regclass AND d.objid = here.oid))
"""
# Everything that references the role in this database (its ACLs, defaults, ownership).
_WEBAUTH_HERE = f"""
WITH r AS (SELECT oid FROM pg_roles WHERE rolname = '{WEBAUTH}'),
here AS (SELECT oid FROM pg_database WHERE datname = current_database())
SELECT count(*) FROM pg_shdepend d, r, here
WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = r.oid
  AND (d.dbid = here.oid OR (d.dbid = 0 AND d.classid = 'pg_database'::regclass
                             AND d.objid = here.oid))
"""
# Relation and schema privileges it holds here, one row per object.
_WEBAUTH_HELD = f"""
WITH r AS (SELECT oid FROM pg_roles WHERE rolname = '{WEBAUTH}')
SELECT DISTINCT CASE c.relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
       quote_ident(n.nspname) || '.' || quote_ident(c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, r
WHERE EXISTS (SELECT 1 FROM aclexplode(c.relacl) a WHERE a.grantee = r.oid)
   OR EXISTS (SELECT 1 FROM pg_attribute att, aclexplode(att.attacl) a
              WHERE att.attrelid = c.oid AND att.attnum > 0 AND a.grantee = r.oid)
UNION
SELECT 'SCHEMA', quote_ident(n.nspname) FROM pg_namespace n, r
WHERE EXISTS (SELECT 1 FROM aclexplode(n.nspacl) a WHERE a.grantee = r.oid)
ORDER BY 1, 2
"""
_WEBAUTH_DEFAULTS = f"""
SELECT quote_ident(pg_get_userbyid(d.defaclrole)),
       CASE WHEN d.defaclnamespace = 0 THEN NULL
            ELSE quote_ident(d.defaclnamespace::regnamespace::text) END,
       d.defaclobjtype
FROM pg_default_acl d, aclexplode(d.defaclacl) a
WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '{WEBAUTH}')
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""
# Another database of the cluster that still references it (``dbid`` 0: a shared object).
_WEBAUTH_ELSEWHERE = f"""
SELECT DISTINCT coalesce(db.datname, '(shared object)') FROM pg_shdepend d
LEFT JOIN pg_database db ON db.oid = d.dbid
WHERE d.refclassid = 'pg_authid'::regclass
  AND d.refobjid = (SELECT oid FROM pg_roles WHERE rolname = '{WEBAUTH}')
ORDER BY 1
"""


def _scalar(sql: str) -> object:
    return op.get_bind().execute(text(sql)).scalar()


def _rows(sql: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in op.get_bind().execute(text(sql)).all()]


def _exists(sql: str) -> bool:
    return bool(_scalar(sql))


def _counts() -> dict[str, int]:
    return {table: int(_scalar(f"SELECT count(*) FROM public.{table}") or 0) for table in LEGACY}


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"LOCK TABLE {', '.join(f'public.{t}' for t in OUTBOXES)} IN SHARE MODE")
    op.execute("LOCK TABLE public.exchange_accounts IN SHARE MODE")
    op.execute("LOCK TABLE public.exchange_account_memberships IN SHARE ROW EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {', '.join(f'public.{t}' for t in LEGACY)} IN SHARE MODE")
    has_auth = _exists(f"SELECT 1 FROM pg_namespace WHERE nspname = '{AUTH}'")
    has_webauth = _exists(f"SELECT 1 FROM pg_roles WHERE rolname = '{WEBAUTH}'")

    if has_webauth and (other := _rows(_WEBAUTH_OTHER)):
        raise RuntimeError(f"refuse: {WEBAUTH} has {len(other)} dependencies besides its "
                           "relation, schema and default privileges in this database")

    # A. every live account keeps an operator keyed by a tailnet (numeric) user ID.
    orphaned = int(_scalar(_ACCOUNTS_WITHOUT_OPERATOR) or 0)
    if orphaned:
        raise RuntimeError(f"refuse: {orphaned} active or halted accounts have no owner/operator "
                           "membership keyed by a numeric user ID")
    # B. no pending request from an old (non-numeric) identity.
    pending = sum(int(_scalar(f"SELECT count(*) FROM public.{table} WHERE state = 'requested' "
                              f"AND requested_by !~ {NUMERIC}") or 0) for table in OUTBOXES)
    if pending:
        raise RuntimeError(f"refuse: {pending} requested operator requests come from a "
                           "non-numeric requested_by")

    # C. the Better Auth memberships, exactly as many as counted.
    legacy_members = int(_scalar("SELECT count(*) FROM public.exchange_account_memberships "
                                 f"WHERE user_id !~ {NUMERIC}") or 0)
    deleted = bind.execute(text("DELETE FROM public.exchange_account_memberships "
                                f"WHERE user_id !~ {NUMERIC}")).rowcount
    if deleted != legacy_members:
        raise RuntimeError(f"refuse: deleted {deleted} non-numeric memberships, "
                           f"counted {legacy_members}")

    before = _counts()
    if has_auth:
        # D. default privileges in auth, so that E sees only object dependencies.
        for grantor, grantee, objtype in _rows(_AUTH_DEFAULTS):
            op.execute(f'ALTER DEFAULT PRIVILEGES FOR ROLE {grantor} IN SCHEMA "{AUTH}" '
                       f"REVOKE ALL ON {_DEFAULT_OBJECTS[str(objtype)]} FROM {grantee}")
        if _rows(_AUTH_DEFAULTS):
            raise RuntimeError(f"refuse: a default privilege in {AUTH} survived the revoke")
        # E. nothing outside auth goes with it, except the one foreign key.
        foreign_keys = _rows(_FOREIGN_KEYS_INTO_AUTH)
        if foreign_keys != [KEPT_FK]:
            raise RuntimeError(f"refuse: the foreign keys into {AUTH} are {foreign_keys}, "
                               f"expected only {KEPT_FK}")
        functions = int(_scalar(_FUNCTIONS_READING_AUTH) or 0)
        if functions:
            raise RuntimeError(f"refuse: {functions} function bodies outside {AUTH} read it")
        dependents = [str(row[0]) for row in _rows(_DEPENDENTS_OUTSIDE_AUTH)]
        if dependents:
            raise RuntimeError(f"refuse: {len(dependents)} objects outside {AUTH} depend on it: "
                               + "; ".join(dependents))
        op.execute(f'DROP SCHEMA "{AUTH}" CASCADE')
    # F. the legacy tables only lost the foreign key.
    after = _counts()
    if after != before:
        raise RuntimeError(f"refuse: legacy row counts changed from {before} to {after}")

    # G. the role, as d3e4f5a6b7c8 drops bfx_cutover_reader.
    if not has_webauth:
        return
    for kind, name in _rows(_WEBAUTH_HELD):
        # A table-level REVOKE also revokes the column privileges on that table.
        op.execute(f"REVOKE ALL ON {kind} {name} FROM {WEBAUTH}")
    for grantor, schema, objtype in _rows(_WEBAUTH_DEFAULTS):
        scoped = "" if schema is None else f" IN SCHEMA {schema}"
        op.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE {grantor}{scoped} "
                   f"REVOKE ALL ON {_DEFAULT_OBJECTS[str(objtype)]} FROM {WEBAUTH}")
    if _scalar(_WEBAUTH_HERE):
        raise RuntimeError(f"refuse: {WEBAUTH} is still referenced in this database "
                           "after the revoke")
    elsewhere = [str(name) for (name,) in _rows(_WEBAUTH_ELSEWHERE)]
    if not elsewhere:
        op.execute(f"DROP ROLE {WEBAUTH}")
        return
    # Another database of this cluster still references it (a test cluster's other databases;
    # never production's one): the role stays, holding nothing here, and says why.
    listed = ", ".join(elsewhere).replace("'", "''")
    op.execute(f"DO $notice$ BEGIN RAISE NOTICE '{WEBAUTH} kept: still referenced by %', "
               f"'{listed}'; END $notice$")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
