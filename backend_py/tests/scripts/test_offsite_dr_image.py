import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_postgres_image_has_the_resolved_digest_and_pgbackrest_checksum() -> None:
    dockerfile = (ROOT / "deploy/vm/postgres/Dockerfile").read_text()
    assert "FROM postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2" in dockerfile
    assert "PG_BACKREST_VERSION=2.59.1" in dockerfile
    assert "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d" in dockerfile
    assert "sha256sum -c" in dockerfile
    assert "meson setup" in dockerfile
    assert "ninja -C" in dockerfile
    assert re.search(r"^RUN .*pgbackrest.* version", dockerfile, re.MULTILINE)


def test_tracked_pgbackrest_config_contains_no_secret_options() -> None:
    config = (ROOT / "deploy/vm/pgbackrest/pgbackrest.conf").read_text()
    for option in ("repo1-s3-key=", "repo1-s3-key-secret=", "repo1-cipher-pass="):
        assert option not in config
    for option in (
        "repo1-type=s3", "repo1-path=/pgbackrest", "repo1-s3-region=auto",
        "repo1-s3-uri-style=path", "repo1-block=y", "repo1-bundle=y",
        "archive-async=y", "start-fast=y", "repo1-cipher-type=aes-256-cbc",
        "repo1-retention-full=4", "repo1-retention-diff=6",
        "pg1-path=/var/lib/postgresql/18/docker",
    ):
        assert option in config


def test_production_postgres_service_uses_archive_image_and_keeps_autoheal_separate() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.bot.yml").read_text())
    postgres = compose["services"]["postgres"]
    assert postgres["image"] == "bfx-postgres:local"
    assert postgres["build"]["dockerfile"] == "deploy/vm/postgres/Dockerfile"
    assert "archive_mode=on" in " ".join(postgres["command"])
    assert "archive_command=pgbackrest --stanza=bfx archive-push %p" in " ".join(postgres["command"])
    assert any("/etc/pgbackrest/pgbackrest.conf" in item for item in postgres["volumes"])
    assert any("/etc/pgbackrest/conf.d" in item for item in postgres["volumes"])
    assert any("/var/spool/pgbackrest" in item for item in postgres["volumes"])
    assert "labels" not in postgres or postgres["labels"].get("autoheal") != "true"
