"""``tests/trigger_columns.py``: the parser the head-schema trigger check relies on.

Each guard has a body that only it catches: dropping the column from the fixture table,
the ``TG_TABLE_NAME`` narrowing, the ``ELSE`` complement, comment / literal stripping and
the CASE frame each turn one of these tests red.
"""
from __future__ import annotations

import pytest

from tests.trigger_columns import column_references, missing_columns

_SINGLE = """
BEGIN
  IF NEW.state <> OLD.state THEN
    NEW.updated_at := now();
  END IF;
  RETURN NEW;
END;
"""


def test_a_column_the_table_has_passes() -> None:
    assert missing_columns(_SINGLE, {"offers": {"id", "state", "updated_at"}}) == {}


def test_a_dropped_column_is_reported() -> None:
    assert missing_columns(_SINGLE, {"offers": {"id", "state"}}) == {"offers": {"updated_at"}}


_SHARED = """
BEGIN
  IF TG_TABLE_NAME = 'a' THEN
    PERFORM NEW.only_a;
  ELSIF TG_TABLE_NAME IN ('b', 'c') THEN
    PERFORM NEW.b_and_c;
  ELSE
    PERFORM NEW.only_d;
  END IF;
  RETURN NEW.common;
END;
"""


def test_a_tg_table_name_branch_is_checked_against_its_tables_only() -> None:
    refs = column_references(_SHARED, ["a", "b", "c", "d"])
    assert refs == {"a": {"only_a", "common"}, "b": {"b_and_c", "common"},
                    "c": {"b_and_c", "common"}, "d": {"only_d", "common"}}


def test_a_column_dropped_from_one_branch_table_is_reported_for_that_table() -> None:
    tables = {"a": {"only_a", "common"}, "b": {"b_and_c", "common"},
              "c": {"common"}, "d": {"only_d", "common"}}
    assert missing_columns(_SHARED, tables) == {"c": {"b_and_c"}}


def test_an_or_that_does_not_pin_every_branch_does_not_narrow() -> None:
    body = "BEGIN IF flag OR TG_TABLE_NAME = 'a' THEN PERFORM NEW.x; END IF; END;"
    assert column_references(body, ["a", "b"]) == {"a": {"x"}, "b": {"x"}}


def test_an_or_of_pinned_branches_narrows_to_their_union() -> None:
    body = """BEGIN IF (TG_TABLE_NAME = 'a' AND done) OR (TG_TABLE_NAME = 'b' AND NOT done)
              THEN PERFORM NEW.x; END IF; END;"""
    assert column_references(body, ["a", "b", "c"]) == {"a": {"x"}, "b": {"x"}, "c": set()}


def test_comments_and_literals_are_not_read() -> None:
    body = """BEGIN
      -- NEW.in_line_comment
      /* NEW.in_block_comment */
      RAISE EXCEPTION 'NEW.in_literal % it''s', NEW.real;
      EXECUTE format($q$UPDATE t SET x = NEW.in_dollar$q$);
    END;"""
    assert column_references(body, ["t"]) == {"t": {"real"}}


def test_a_case_else_inside_a_pinned_branch_stays_in_that_branch() -> None:
    body = """BEGIN
      IF TG_TABLE_NAME = 'a' THEN
        PERFORM CASE WHEN NEW.x THEN 1 ELSE 2 END;
        PERFORM NEW.after_case;
      END IF;
    END;"""
    assert column_references(body, ["a", "b"]) == {"a": {"x", "after_case"}, "b": set()}


def test_quoted_and_lowercase_references() -> None:
    body = 'BEGIN PERFORM new.Lower, OLD."MixedCase", NEW . spaced; END;'
    assert column_references(body, ["t"]) == {"t": {"lower", "MixedCase", "spaced"}}


def test_a_negated_pin_does_not_narrow() -> None:
    body = """BEGIN IF NOT (TG_TABLE_NAME = 'a') THEN PERFORM NEW.x;
              ELSE PERFORM NEW.y; END IF; END;"""
    assert column_references(body, ["a", "b"]) == {"a": {"x", "y"}, "b": {"x", "y"}}


def test_a_pin_nested_under_and_or_does_not_narrow() -> None:
    body = "BEGIN IF y AND (z OR TG_TABLE_NAME = 'a') THEN PERFORM NEW.x; END IF; END;"
    assert column_references(body, ["a", "b"]) == {"a": {"x"}, "b": {"x"}}


def test_a_nested_begin_end_inside_case_does_not_close_the_case() -> None:
    body = """BEGIN
      IF TG_TABLE_NAME = 'a' THEN
        CASE WHEN true THEN BEGIN NULL; END;
        ELSE PERFORM NEW.z;
        END CASE;
      END IF;
    END;"""
    assert column_references(body, ["a", "b"]) == {"a": {"z"}, "b": set()}


def test_pins_match_the_relname_of_a_schema_qualified_key() -> None:
    body = "BEGIN IF TG_TABLE_NAME = 'a' THEN PERFORM NEW.x; END IF; END;"
    tables: dict[str, set[str]] = {"s1.a": set(), "s2.b": set()}
    assert missing_columns(body, tables, {"s1.a": "a", "s2.b": "b"}) == {"s1.a": {"x"}}


def test_a_nested_begin_end_inside_an_if_branch_keeps_the_else() -> None:
    body = """BEGIN
      IF TG_TABLE_NAME = 'a' THEN BEGIN NULL; END;
      ELSE PERFORM NEW.y;
      END IF;
    END;"""
    assert column_references(body, ["a", "b"]) == {"a": set(), "b": {"y"}}


def test_an_elsif_directly_inside_a_begin_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="ELSIF outside an IF"):
        column_references("BEGIN ELSIF x THEN NULL; END;", ["t"])
