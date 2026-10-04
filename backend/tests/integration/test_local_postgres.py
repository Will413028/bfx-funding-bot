"""The per-process PostgreSQL: production's major version and collation behaviour."""
from __future__ import annotations

import psycopg
import pytest

from tests import pg_local

pytestmark = pytest.mark.integration


def _scalar(server: pg_local.LocalPostgres, sql: str, dbname: str = "postgres") -> object:
    with psycopg.connect(host=server.host, port=server.port, user=server.username,
                         password=server.password, dbname=dbname, autocommit=True) as connection:
        return connection.execute(sql).fetchone()[0]


def test_server_is_the_major_pinned_by_the_production_dockerfile(pg_server: pg_local.LocalPostgres) -> None:
    major = int(_scalar(pg_server, "SHOW server_version_num")) // 10000
    assert major == pg_local.dockerfile_pg_major() == 18


def test_collation_is_the_builtin_c_utf8_provider_in_every_new_database(
    pg_server: pg_local.LocalPostgres,
) -> None:
    assert _scalar(pg_server, "SELECT datlocprovider FROM pg_database WHERE datname = 'template1'") == "b"
    assert _scalar(pg_server, "SELECT datlocale FROM pg_database WHERE datname = 'template1'") == "C.UTF-8"
    assert _scalar(pg_server, "SELECT pg_encoding_to_char(encoding) FROM pg_database "
                              "WHERE datname = 'template1'") == "UTF8"
    # Code-point order (what production's libc en_US.utf8 on musl gives), not locale order.
    ordered = _scalar(pg_server, "SELECT string_agg(v, ',' ORDER BY v) FROM "
                                 "(VALUES ('b'), ('B'), ('a'), ('A')) AS t(v)")
    assert ordered == "A,B,a,b"


def test_server_renders_timestamps_in_utc_like_the_production_container(
    pg_server: pg_local.LocalPostgres,
) -> None:
    assert _scalar(pg_server, "SHOW timezone") == "UTC"


def test_server_lives_in_its_own_temp_directory_and_a_short_socket_path(
    pg_server: pg_local.LocalPostgres,
) -> None:
    assert pg_server.data_directory.is_relative_to(pg_server.temp_root)
    assert str(_scalar(pg_server, "SHOW data_directory")) == str(pg_server.data_directory)
    socket_path = pg_server.socket_directory / f".s.PGSQL.{pg_server.port}"
    assert len(str(socket_path)) < 100
    assert socket_path.exists()
    assert 1024 < pg_server.port < 65536
