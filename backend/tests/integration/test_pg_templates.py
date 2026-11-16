"""TemplateDatabases refuses a build that leaves a connection open to its template.

``CREATE DATABASE ... TEMPLATE`` fails with ``ObjectInUse`` while anyone is connected to the
source, and an undisposed engine keeps its pooled connection until the garbage collector frees
it, so such a leak made every clone fail at random (CI run 43455136487). The guard turns that
into a deterministic failure of the build itself.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

pytestmark = pytest.mark.integration


def test_a_build_that_leaks_a_connection_is_refused(pg_templates) -> None:
    leaked = []

    def build(url: str) -> None:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        leaked.append(engine)  # checked back into the pool, never disposed

    try:
        with pytest.raises(RuntimeError, match="left 1 connection"):
            pg_templates.template("leaky_template_guard", build)
    finally:
        for engine in leaked:
            engine.dispose()
    with pytest.raises(KeyError):
        pg_templates.built("leaky_template_guard")


def test_a_build_that_disposes_its_engine_can_be_cloned(pg_templates, pg_clone) -> None:
    def build(url: str) -> None:
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE t (x int)")
        finally:
            engine.dispose()

    pg_clone(pg_templates.template("tidy_template_guard", build))
