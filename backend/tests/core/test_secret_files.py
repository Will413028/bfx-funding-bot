"""core/secret_files.py, the one reader of the resident services' secrets, and its consumers.

Each test writes real files and points BFX_SECRETS_DIR at them, as Compose mounts them at
/run/secrets. Mutations (one at a time; revert after each): read the env before the file in
read_secret (test_file_mode_reads_the_file_and_ignores_the_env fails); return None for a
missing file (test_file_mode_refuses_a_missing_empty_or_unreadable_file fails); make
load_kek read os.environ (test_the_vault_key_is_read_from_its_file fails); build a bare
``Settings()`` in g3.py (test_settings_with_a_file_secret_are_built_only_through_the_reader
fails).
"""
from __future__ import annotations

import ast
import base64
from pathlib import Path

import pytest

from bfx_funding_bot.apps.bot import admin_tokens
from bfx_funding_bot.apps.venue import _throwaway_credentials
from bfx_funding_bot.core.crypto import VaultNotConfiguredError, load_kek
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.core.secret_files import (
    FILE_SECRET_KEYS,
    SecretFileError,
    file_secret_values,
    ignored_env_keys,
    read_secret,
    secret_file_mounted,
    secret_present,
)
from bfx_funding_bot.core.settings import load_settings

KEK = base64.b64encode(bytes(range(32))).decode()
OTHER_KEK = base64.b64encode(bytes(range(1, 33))).decode()
SRC = Path(__file__).resolve().parents[2] / "src" / "bfx_funding_bot"


def _dir(tmp_path: Path, **values: str) -> dict[str, str]:
    directory = tmp_path / "run-secrets"
    directory.mkdir(exist_ok=True)
    for key, value in values.items():
        (directory / key).write_text(value)
    return {"BFX_SECRETS_DIR": str(directory)}


# --------------------------------------------------------------------------- the reader


def test_env_mode_reads_the_env_as_before() -> None:
    assert read_secret("BFX_ADMIN_TOKEN", {"BFX_ADMIN_TOKEN": " t \n"}) == "t"
    assert read_secret("BFX_ADMIN_TOKEN", {"BFX_ADMIN_TOKEN": "  "}) is None
    assert read_secret("BFX_ADMIN_TOKEN", {}) is None
    assert read_secret("BFX_ADMIN_TOKEN", {"BFX_SECRETS_DIR": " ", "BFX_ADMIN_TOKEN": "t"}) == "t"


def test_file_mode_reads_the_file_and_ignores_the_env(tmp_path: Path) -> None:
    env = _dir(tmp_path, BFX_ADMIN_TOKEN="from-file\n") | {"BFX_ADMIN_TOKEN": "from-env"}
    assert read_secret("BFX_ADMIN_TOKEN", env) == "from-file"


@pytest.mark.parametrize("state", ["missing", "empty", "blank", "not_utf8", "directory"])
def test_file_mode_refuses_a_missing_empty_or_unreadable_file(tmp_path: Path, state: str) -> None:
    env = _dir(tmp_path) | {"DATABASE_URL": "postgresql://env-is-no-fallback"}
    path = Path(env["BFX_SECRETS_DIR"]) / "DATABASE_URL"
    if state in ("empty", "blank"):
        path.write_text({"empty": "", "blank": " \n\t"}[state])
    if state == "not_utf8":
        path.write_bytes(b"secret-value\xff")
    if state == "directory":
        path.mkdir()
    expected = {"missing": "is missing", "empty": "is empty", "blank": "is empty",
                "not_utf8": "is unreadable", "directory": "is unreadable"}[state]
    with pytest.raises(SecretFileError, match=f"secret file .*/DATABASE_URL {expected}") as raised:
        read_secret("DATABASE_URL", env)
    assert "secret-value" not in str(raised.value) and "env-is-no-fallback" not in str(raised.value)
    assert isinstance(raised.value, ValueError)  # the bot's boot: config_fatal, exit 1


def test_a_relative_secrets_dir_is_refused() -> None:
    with pytest.raises(SecretFileError, match="absolute"):
        read_secret("DATABASE_URL", {"BFX_SECRETS_DIR": "run/secrets"})


def test_only_the_known_secrets_are_read() -> None:
    with pytest.raises(KeyError):
        read_secret("SENTRY_DSN", {"SENTRY_DSN": "x"})


def test_presence_counts_an_empty_env_key_or_an_empty_file(tmp_path: Path) -> None:
    assert secret_present("BFX_VAULT_KEK", {"BFX_VAULT_KEK": ""})
    assert not secret_present("BFX_VAULT_KEK", {})
    env = _dir(tmp_path, BFX_VAULT_KEK="")
    assert secret_present("BFX_VAULT_KEK", env) and secret_file_mounted("BFX_VAULT_KEK", env)
    assert not secret_present("BFX_ADMIN_TOKEN", env)
    (Path(env["BFX_SECRETS_DIR"]) / "BFX_ADMIN_TOKEN").symlink_to(tmp_path / "nowhere")
    assert secret_present("BFX_ADMIN_TOKEN", env)
    assert not secret_file_mounted("BFX_VAULT_KEK", {"BFX_VAULT_KEK": "x"})


def test_file_values_for_scrubbing_never_raise(tmp_path: Path) -> None:
    env = _dir(tmp_path, BFX_ADMIN_TOKEN="a\n", TELEGRAM_BOT_TOKEN="", DATABASE_URL="u")
    assert file_secret_values(env) == {"BFX_ADMIN_TOKEN": "a", "DATABASE_URL": "u"}
    assert file_secret_values({"BFX_ADMIN_TOKEN": "a"}) == {}
    assert file_secret_values({"BFX_SECRETS_DIR": "relative"}) == {}


def test_ignored_env_keys_name_what_is_left_in_the_env_file(tmp_path: Path) -> None:
    env = _dir(tmp_path) | {"DATABASE_URL": "x", "BFX_VAULT_KEK": "", "SENTRY_DSN": "d"}
    assert ignored_env_keys(env) == ("BFX_VAULT_KEK", "DATABASE_URL")
    assert ignored_env_keys({"DATABASE_URL": "x"}) == ()


# --------------------------------------------------------------------------- consumers


def test_the_vault_key_is_read_from_its_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _dir(tmp_path, BFX_VAULT_KEK=KEK + "\n")
    monkeypatch.setenv("BFX_SECRETS_DIR", env["BFX_SECRETS_DIR"])
    monkeypatch.setenv("BFX_VAULT_KEK", OTHER_KEK)
    assert load_kek() == bytes(range(32))
    (Path(env["BFX_SECRETS_DIR"]) / "BFX_VAULT_KEK").unlink()
    with pytest.raises(VaultNotConfiguredError, match="BFX_VAULT_KEK is missing"):
        load_kek()


def test_the_bots_admin_tokens_are_read_from_their_files(tmp_path: Path) -> None:
    env = _dir(tmp_path, BFX_ADMIN_TOKEN="halt\n", BFX_ADMIN_READ_TOKEN="read\n")
    assert admin_tokens(env | {"BFX_ADMIN_TOKEN": "env-halt"}) == ("halt", "read")
    (Path(env["BFX_SECRETS_DIR"]) / "BFX_ADMIN_READ_TOKEN").write_text("halt")
    assert admin_tokens(env) == ("halt", None)  # equal to the halt token: dropped, as before
    (Path(env["BFX_SECRETS_DIR"]) / "BFX_ADMIN_TOKEN").unlink()
    with pytest.raises(SecretFileError, match="BFX_ADMIN_TOKEN is missing"):
        admin_tokens(env)


def test_the_web_apis_settings_take_the_database_url_from_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _dir(tmp_path, DATABASE_URL="postgresql://bfx_webapi:p@bfx-postgres:5432/bfx\n")
    monkeypatch.setenv("BFX_SECRETS_DIR", env["BFX_SECRETS_DIR"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://bfx_bot:wrong@bfx-postgres:5432/bfx")
    assert load_settings().database_url == "postgresql://bfx_webapi:p@bfx-postgres:5432/bfx"
    monkeypatch.delenv("BFX_SECRETS_DIR")
    assert load_settings().database_url == "postgresql://bfx_bot:wrong@bfx-postgres:5432/bfx"


@pytest.mark.parametrize("content", ["", KEK])
def test_a_simulated_boot_refuses_a_mounted_vault_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str,
) -> None:
    monkeypatch.delenv("BFX_VAULT_KEK", raising=False)
    monkeypatch.setenv("BFX_SECRETS_DIR", _dir(tmp_path, BFX_VAULT_KEK=content)["BFX_SECRETS_DIR"])
    with pytest.raises(ConfigurationError, match="BFX_VAULT_KEK must not be set or mounted"):
        _throwaway_credentials()


# --------------------------------------------------------------------------- one reader

# One-shot processes that stay on env by decision (D15): the DR restore check and the
# operator CLI read their DATABASE_URL straight from their env file.
ONE_SHOT_ENV_READERS = {"apps/restore_boot_check.py", "apps/operator_cli.py"}
# Not a read of the secret: the web API refuses to start with the halt token in its env
# (a non-empty value; the file is checked through the reader); the alert/Sentry scrubber
# covers a stale env value as well as the file's (secret_values).
NOT_READS = {"apps/webapi.py": {"BFX_ADMIN_TOKEN"},
             "modules/observability/alerts.py": {"DATABASE_URL"}}


def _direct_env_reads(tree: ast.AST) -> list[str]:
    """String literals naming a file secret used as an env lookup: environ.get("K"),
    environ["K"], getenv("K"), env.get("K") on any mapping named *env*, "K" in environ."""
    found = []
    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Call) and node.args and (
                (isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "getenv", "pop"))
                or (isinstance(node.func, ast.Name) and node.func.id == "getenv")):
            key = node.args[0]
        elif isinstance(node, ast.Subscript):
            key = node.slice
        elif isinstance(node, ast.Compare) and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            key = node.left
        if isinstance(key, ast.Constant) and key.value in FILE_SECRET_KEYS:
            found.append(f"{key.value}@{node.lineno}")
    return found


def test_no_resident_code_reads_a_file_secret_from_the_env() -> None:
    """Every read of these keys goes through core/secret_files.py, so file mode cannot be
    bypassed by a consumer that still looks at os.environ (the 9h 'one reader' rule)."""
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative == "core/secret_files.py" or relative in ONE_SHOT_ENV_READERS:
            continue
        reads = [read for read in _direct_env_reads(ast.parse(path.read_text(encoding="utf-8")))
                 if read.partition("@")[0] not in NOT_READS.get(relative, set())]
        if reads:
            offenders[relative] = reads
    assert offenders == {}


def _secret_settings_classes(tree: ast.AST) -> set[str]:
    """pydantic BaseSettings subclasses with a field that names a file secret (by field
    name or a string validation_alias/alias): building one fills it from the env."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not any(
                (base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")) == "BaseSettings"
                for base in node.bases):
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue
            names = {item.target.id.upper()}
            if isinstance(item.value, ast.Call):
                names |= {kw.value.value for kw in item.value.keywords
                          if kw.arg in ("alias", "validation_alias")
                          and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)}
            if names & FILE_SECRET_KEYS:
                found.add(node.name)
    return found


def _constructions(tree: ast.AST, classes: set[str]) -> list[str]:
    return [f"{name}@{node.lineno}" for node in ast.walk(tree) if isinstance(node, ast.Call)
            for name in [node.func.id if isinstance(node.func, ast.Name)
                         else getattr(node.func, "attr", None)] if name in classes]


def test_settings_with_a_file_secret_are_built_only_through_the_reader() -> None:
    """pydantic-settings reads DATABASE_URL from the env on its own: a bare ``Settings()``
    in file mode would take a leftover env value. Only core/settings.py (load_settings,
    which passes the reader's value) may build such a class; everything else calls
    load_settings."""
    trees = {path.relative_to(SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
             for path in sorted(SRC.rglob("*.py"))}
    classes = set().union(*(_secret_settings_classes(tree) for tree in trees.values()))
    assert "Settings" in classes                    # the scan sees the real class
    offenders = {relative: found for relative, tree in trees.items()
                 if relative != "core/settings.py" and (found := _constructions(tree, classes))}
    assert offenders == {}


def test_the_settings_scan_sees_a_secret_field_and_a_construction() -> None:
    source = '\n'.join([
        "class A(BaseSettings):", "    database_url: str", "class B(pydantic_settings.BaseSettings):",
        "    token: str = Field(validation_alias='BFX_ADMIN_TOKEN')", "class C(BaseSettings):",
        "    log_level: str = 'INFO'", "a = A()", "b = mod.B(x=1)", "c = C()"])
    tree = ast.parse(source)
    assert _secret_settings_classes(tree) == {"A", "B"}
    assert _constructions(tree, {"A", "B"}) == ["A@7", "B@8"]


def test_the_scan_sees_each_way_of_reading_the_env() -> None:
    source = '\n'.join([
        'os.environ.get("DATABASE_URL")', 'os.environ["BFX_VAULT_KEK"]', 'os.getenv("BFX_ADMIN_TOKEN")',
        'env.get("TELEGRAM_BOT_TOKEN", "")', '"BFX_VAULT_KEK" in os.environ', 'getenv("DATABASE_URL")',
        'environ.get("SENTRY_DSN")'])
    assert len(_direct_env_reads(ast.parse(source))) == 6
