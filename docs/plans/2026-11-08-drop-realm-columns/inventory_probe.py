"""One-off probe for the drop-realm-columns plan: migrate an empty DB to head, dump every
object that touches ``deployment_environment``.

Run from backend/ (uses the repo's local PG18 fixtures):
  PYTHONPATH=. uv run pytest -p tests.conftest --rootdir=. -q -p no:cacheprovider \
    ../docs/plans/2026-11-08-drop-realm-columns/inventory_probe.py
Writes inventory.tsv next to this file. Not part of CI.
"""
from pathlib import Path

import psycopg

ARCHIVE_SCHEMAS = {"legacy_archive", "release_archive"}
OUT = Path(__file__).with_name("inventory.tsv")

QUERY = r"""
WITH cols AS (
  SELECT c.oid AS relid, n.nspname AS schema, c.relname AS tbl, a.attnum, a.attnotnull,
         pg_get_expr(d.adbin, d.adrelid) AS dflt
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
  WHERE a.attname = 'deployment_environment' AND c.relkind IN ('r','p') AND NOT a.attisdropped
)
SELECT schema, tbl, 'column', 'deployment_environment',
       CASE WHEN attnotnull THEN 'NOT NULL' ELSE 'NULL' END || ' default=' || coalesce(dflt,'-')
FROM cols
UNION ALL
SELECT cols.schema, cols.tbl,
       CASE k.contype WHEN 'p' THEN 'PK' WHEN 'u' THEN 'UNIQUE' WHEN 'f' THEN 'FK' WHEN 'c' THEN 'CHECK' WHEN 'x' THEN 'EXCLUDE' END,
       k.conname, pg_get_constraintdef(k.oid)
FROM cols JOIN pg_constraint k ON k.conrelid = cols.relid
WHERE k.contype IN ('p','u','f','c','x')
  AND (cols.attnum = ANY(k.conkey) OR pg_get_constraintdef(k.oid) ~ 'deployment_environment')
UNION ALL
-- FKs from other tables that reference a key containing the column (referenced side)
SELECT cols.schema, cols.tbl, 'FK-target', k.conname,
       k.conrelid::regclass::text || ' ' || pg_get_constraintdef(k.oid)
FROM cols JOIN pg_constraint k ON k.confrelid = cols.relid AND k.contype = 'f'
WHERE cols.attnum = ANY(k.confkey)
UNION ALL
SELECT cols.schema, cols.tbl,
       CASE WHEN i.indisunique AND i.indpred IS NOT NULL THEN 'partial-unique-index'
            WHEN i.indisunique THEN 'unique-index' ELSE 'index' END,
       ic.relname, pg_get_indexdef(i.indexrelid)
FROM cols JOIN pg_index i ON i.indrelid = cols.relid
JOIN pg_class ic ON ic.oid = i.indexrelid
WHERE (cols.attnum = ANY(i.indkey::int2[]) OR pg_get_indexdef(i.indexrelid) ~ 'deployment_environment')
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid AND k.contype IN ('p','u','x'))
UNION ALL
SELECT cols.schema, cols.tbl, 'trigger', t.tgname, pg_get_triggerdef(t.oid)
FROM cols JOIN pg_trigger t ON t.tgrelid = cols.relid
WHERE NOT t.tgisinternal AND (t.tgname = 'database_realm_write'
      OR t.tgfoid = 'guard_database_realm'::regproc)
UNION ALL
-- other trigger functions whose body reads the column (plpgsql binds late: they fail at run
-- time once the column is gone)
SELECT cols.schema, cols.tbl, 'trigger-function', t.tgname,
       pn.nspname || '.' || p.proname || '() via ' || pg_get_triggerdef(t.oid)
FROM cols JOIN pg_trigger t ON t.tgrelid = cols.relid
JOIN pg_proc p ON p.oid = t.tgfoid JOIN pg_namespace pn ON pn.oid = p.pronamespace
WHERE NOT t.tgisinternal AND p.prosrc ~ 'deployment_environment'
  AND t.tgfoid <> 'guard_database_realm'::regproc
ORDER BY 1, 2, 3, 4
"""


def destination(schema: str, kind: str, name: str, definition: str) -> str:
    if schema in ARCHIVE_SCHEMAS:
        if kind == "trigger":
            return "刪除（步驟 4：DROP FUNCTION guard_database_realm 之前先 DROP TRIGGER；不改封存列）"
        if kind == "trigger-function":
            return "保留（封存表自己的 guard，只比對該表 NEW／OLD 的本欄；欄位保留所以不動）"
        return "保留（封存表：manifest SHA-256 涵蓋 to_jsonb(row)，欄位與約束不動）"
    if kind == "trigger-function":
        return "改寫（步驟 4：DROP COLUMN 之前 CREATE OR REPLACE 這個 function，本體不再讀本欄）"
    if kind == "column":
        return "刪除（步驟 4：DROP COLUMN）"
    if kind == "trigger":
        return "刪除（步驟 4：DROP TRIGGER，最後 DROP FUNCTION guard_database_realm）"
    if kind == "PK":
        return "改寫（步驟 4：隨 DROP COLUMN 消失，以原名重建窄版）"
    if kind in ("UNIQUE", "unique-index", "partial-unique-index"):
        return "改寫（步驟 4：隨 DROP COLUMN 消失，以原名重建窄版）"
    if kind == "FK":
        return "改寫（步驟 4：先 DROP，刪欄後以原名重建）"
    if kind == "FK-target":
        return "改寫（隨被引用 key，見該 FK 列）"
    if kind == "CHECK":
        if "deployment_environment = 'ci'" in definition:
            return "刪除（步驟 4：DROP COLUMN 之前先 DROP，不重建；sim 只寫 ci 庫由 store_sql._check_database_realm 守住，已存在）"
        return "改寫（步驟 4：DROP COLUMN 之前先 DROP，刪欄後同名重建，去掉 deployment_environment 條件）"
    if kind == "index":
        if name == "ix_ledger_observation_query_scope_revision":
            return ("刪除（步驟 4：隨 DROP COLUMN 消失，不重建：與重建後的窄 UNIQUE "
                    "uq_ledger_observation_query_scope_revision 欄位相同只差方向，btree 反向掃描即可）")
        if "(deployment_environment)" in definition:
            return "刪除（步驟 4：只索引本欄，隨 DROP COLUMN 消失）"
        return "改寫（步驟 4：隨 DROP COLUMN 消失，以原名重建窄版）"
    return ""  # unknown kind -> blank, caught by check_inventory.py


def test_dump_inventory(pg_head_url: str) -> None:
    url = pg_head_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        head = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        rows = conn.execute(QUERY).fetchall()
    lines = [f"# alembic head {head}; generated by inventory_probe.py",
             "schema\ttable\tkind\tname\tdefinition\t去向"]
    for schema, tbl, kind, name, definition in rows:
        definition = " ".join(definition.split())
        lines.append("\t".join([schema, tbl, kind, name, definition,
                                destination(schema, kind, name, definition)]))
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert rows
