"""Which ``NEW.<col>`` / ``OLD.<col>`` columns a plpgsql trigger body reads, per table.

Dropping or renaming a column does not fail the migration that does it: a plpgsql body
is only resolved when it runs, so the trigger breaks on its first fire in production.
``missing_columns`` lists, for one trigger function and the tables it is attached to,
every ``NEW``/``OLD`` field the body names that the table does not have.

A function shared by several tables branches on ``TG_TABLE_NAME``; a reference inside
``IF TG_TABLE_NAME = 'x'`` (or ``IN ('x', 'y')``) is checked against those tables only,
and a later ``ELSIF``/``ELSE`` against the attached tables its earlier siblings did not
pin. Comments and string literals (including dynamic ``EXECUTE`` text) are not read.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_FIELD_REF = re.compile(rf"\b(NEW|OLD)\s*\.\s*({_IDENT})", re.IGNORECASE)
_KEYWORD = re.compile(
    r"\b(END\s+IF|END\s+CASE|END\s+LOOP|END|CASE|ELSIF|ELSEIF|ELSE|IF|THEN)\b", re.IGNORECASE)
_CASE: list[set[str]] = []  # stack marker: a CASE frame, whose ELSE is not an IF's
_PIN = re.compile(r"\bTG_TABLE_NAME\s*(?:=\s*'#(\d+)'|IN\s*\(([^()]*)\))", re.IGNORECASE)
_LITERAL_MARK = re.compile(r"'#(\d+)'")


def _lex(src: str) -> tuple[str, list[str]]:
    """``src`` with comments removed and each string literal replaced by ``'#<n>'``."""
    out: list[str] = []
    literals: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("--", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c == "'" or (c in "eE" and src.startswith("'", i + 1) and not _word_before(src, i)):
            j = i + (2 if c != "'" else 1)
            buf: list[str] = []
            while j < n:
                if src[j] == "\\" and c != "'":
                    buf.append(src[j:j + 2])
                    j += 2
                elif src.startswith("''", j):
                    buf.append("'")
                    j += 2
                elif src[j] == "'":
                    j += 1
                    break
                else:
                    buf.append(src[j])
                    j += 1
            out.append(f"'#{len(literals)}'")
            literals.append("".join(buf))
            i = j
        elif c == "$" and (m := re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", src[i:])) \
                and not _word_before(src, i):
            tag = m.group(0)
            j = src.find(tag, i + len(tag))
            end = n if j < 0 else j + len(tag)
            out.append(f"'#{len(literals)}'")
            literals.append(src[i + len(tag):j if j >= 0 else n])
            i = end
        elif c == '"':
            j = i + 1
            while j < n:
                if src.startswith('""', j):
                    j += 2
                elif src[j] == '"':
                    j += 1
                    break
                else:
                    j += 1
            out.append(src[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out), literals


def _word_before(src: str, i: int) -> bool:
    return i > 0 and (src[i - 1].isalnum() or src[i - 1] in "_$")


def _column(ident: str) -> str:
    if ident.startswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident.lower()


def _split_or(cond: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for m in re.finditer(r"\(|\)|\bOR\b", cond, re.IGNORECASE):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            parts.append(cond[start:m.start()])
            start = m.end()
    parts.append(cond[start:])
    return parts


def _strip_parens(cond: str) -> str:
    cond = cond.strip()
    while cond.startswith("(") and cond.endswith(")"):
        depth = 0
        for k, ch in enumerate(cond):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and k < len(cond) - 1:
                return cond
        cond = cond[1:-1].strip()
    return cond


def _pinned(cond: str, literals: list[str]) -> set[str] | None:
    """Tables ``cond`` can only be true for, or None when it does not pin ``TG_TABLE_NAME``.

    Every top-level ``OR`` branch must pin a name; ``x OR TG_TABLE_NAME = 'a'`` pins nothing.
    """
    names: set[str] = set()
    for part in _split_or(_strip_parens(cond)):
        pins = list(_PIN.finditer(part))
        if not pins:
            return None
        for p in pins:
            if p.group(1) is not None:
                names.add(literals[int(p.group(1))])
            else:
                names.update(literals[int(k)] for k in _LITERAL_MARK.findall(p.group(2)))
    return names


def column_references(prosrc: str, tables: Iterable[str]) -> dict[str, set[str]]:
    """For each attached table, the ``NEW``/``OLD`` columns the body reads on it."""
    attached = set(tables)
    code, literals = _lex(prosrc)
    refs: dict[str, set[str]] = {t: set() for t in attached}
    # Each open IF: [scope of the whole IF, tables its earlier branches pinned]; or a CASE.
    stack: list[list[set[str]]] = []
    scope = set(attached)
    pos = 0
    tokens = list(_KEYWORD.finditer(code))
    k = 0

    def collect(upto: int) -> None:
        for m in _FIELD_REF.finditer(code, pos, upto):
            for t in scope:
                refs[t].add(_column(m.group(2)))

    while k < len(tokens):
        tok = tokens[k]
        word = re.sub(r"\s+", " ", tok.group(1).upper())
        collect(tok.start())
        pos = tok.end()
        k += 1
        if word in {"IF", "ELSIF", "ELSEIF"}:
            then = next((j for j in range(k, len(tokens))
                         if tokens[j].group(1).upper() == "THEN"), None)
            if then is None:
                break
            if word == "IF":
                stack.append([set(scope), set()])
            elif not stack or stack[-1] is _CASE:
                raise ValueError(f"ELSIF outside an IF at offset {tok.start()}")
            frame = stack[-1]
            available = frame[0] - frame[1]
            scope = available  # NEW.x in the condition itself is read on every table it can see
            collect(tokens[then].start())
            pins = _pinned(code[pos:tokens[then].start()], literals)
            if pins is not None:
                scope = available & pins
                frame[1] |= pins
            pos = tokens[then].end()
            k = then + 1
        elif word == "CASE":
            stack.append(_CASE)
        elif word in {"END", "END CASE"} and stack and stack[-1] is _CASE:
            stack.pop()
        elif word == "ELSE" and stack and stack[-1] is not _CASE:
            scope = stack[-1][0] - stack[-1][1]
        elif word == "END IF" and stack and stack[-1] is not _CASE:
            scope = stack.pop()[0]
    collect(len(code))
    return refs


def missing_columns(prosrc: str, table_columns: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """Per attached table, the referenced ``NEW``/``OLD`` columns it does not have."""
    refs = column_references(prosrc, table_columns)
    return {t: cols - table_columns[t] for t, cols in refs.items() if cols - table_columns[t]}
