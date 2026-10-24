"""deploy/vm/ops/bfx_public_site.py -- building the public proof site and uploading it.

The root build step runs this run's one-shot build container on the weekly job's
plan; the unprivileged publish step holds the Cloudflare token and nothing else: it
publishes exactly that build, refuses an unexpected one, and exits 0 only once Pages
serves what it uploaded.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "cf-token-" + "x" * 40
JWT = "upload-jwt-" + "y" * 40


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


site = _load("vm_ops_public_site_under_test", ROOT / "deploy/vm/ops/bfx_public_site.py")
weekly = _load("vm_ops_weekly_report_for_public_site", ROOT / "deploy/vm/ops/bfx_weekly_report.py")
CONFIG = site.Config(account_id="acc", project="borrowrate", token=TOKEN)


def _build(tmp_path: Path) -> Path:
    out = tmp_path / "public-site"
    generated = datetime.now(UTC).isoformat()
    for name in site.SITE_FILES:
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"generated_at": generated}) if name == "proof-summary.json" else f"<{name}>"
        path.write_text(body)
    return out


class FakePages:
    """Cloudflare's API and the served site; records every request."""

    def __init__(self, *, have: set[str] = frozenset(), fail: str | None = None,
                 serves: Mapping[str, bytes] | None = None, csp: str = "default-src 'none'") -> None:
        self.have, self.fail, self.serves, self.csp = set(have), fail, serves, csp
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str],
                 body: bytes | None) -> tuple[int, bytes, Mapping[str, str]]:
        self.requests.append((method, url, dict(headers), body))
        step = url.split("?")[0].rsplit("/", 1)[-1]
        if step == self.fail:
            return 403, json.dumps({"success": False, "errors": [{"code": 10000}]}).encode(), {}
        if url.startswith("https://borrowrate.pages.dev"):
            if url.split("?")[0].endswith("/proof-summary.json"):
                return 200, (self.serves or {}).get("proof-summary.json", b""), {}
            return 200, b"<html>", {"Content-Security-Policy": self.csp}
        result: object = {
            "upload-token": {"jwt": JWT},
            "check-missing": [k for k in json.loads(body or b"{}").get("hashes", []) if k not in self.have]
            if step == "check-missing" else None,
            "upload": None, "upsert-hashes": None, "deployments": {"id": "dep-1"},
        }[step]
        return 200, json.dumps({"success": True, "result": result}).encode(), {}

    def calls(self, step: str) -> list[tuple[str, str, dict[str, str], bytes | None]]:
        return [r for r in self.requests if r[1].split("?")[0].endswith("/" + step)]


def test_the_publish_step_reads_the_directory_the_build_container_writes() -> None:
    compose = yaml.safe_load((ROOT / "deploy/vm/ops" / site.COMPOSE_FILE).read_text())
    service = compose["services"][site.SERVICE]
    assert compose["name"] == site.PROJECT
    assert [(v["source"], v["target"]) for v in service["volumes"]] == [(str(site.BUILD_DIR), "/out")]
    assert service["command"][2].endswith("python -m scripts.build_public_site --out /out/site")
    assert site.SITE_DIR == site.BUILD_DIR / "site"
    unit = (ROOT / "deploy/vm/systemd/bfx-public-site.service").read_text()
    assert f"RuntimeDirectory={site.BUILD_DIR.parent.name}\n" in unit
    assert f"LoadCredential={site.CREDENTIAL}:/opt/bfx/runtime/{site.TOKEN_FILE}\n" in unit
    assert f"LoadCredential={site.CONFIG_CREDENTIAL}:/opt/bfx/runtime/{site.CONFIG_FILE}\n" in unit
    assert "\nEnvironmentFile=" not in unit  # it would reach the root step's interpreter too


def test_collect_returns_exactly_the_site_files(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path))
    assert tuple(files) == site.SITE_FILES


@pytest.mark.parametrize("mutate, code", [
    (lambda d: (d / "extra.js").write_text("x"), "site_files_mismatch:extra=extra.js:missing="),
    (lambda d: (d / "en/index.html").unlink(), "site_files_mismatch:extra=:missing=en/index.html"),
    (lambda d: ((d / "index.html").unlink(), (d / "index.html").symlink_to("/etc/passwd")),
     "site_symlink:index.html"),
])
def test_collect_refuses_anything_but_the_site(tmp_path: Path, mutate, code: str) -> None:
    out = _build(tmp_path)
    mutate(out)
    with pytest.raises(site.PublishError) as caught:
        site.collect(out)
    assert caught.value.code == code


def test_collect_never_follows_a_symlinked_directory(tmp_path: Path) -> None:
    # A same-named file behind the link: only O_NOFOLLOW on the directory stops the read.
    out = _build(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "index.html").write_text("ROOT-ONLY")
    shutil.rmtree(out / "en")
    (out / "en").symlink_to(elsewhere)
    with pytest.raises(site.PublishError) as caught:
        site.collect(out)
    assert caught.value.code == "site_symlink:en"


def test_collect_reports_an_unopenable_entry_as_unreadable(tmp_path: Path) -> None:
    out = _build(tmp_path)
    (out / "_headers").chmod(0)  # the suite never runs as root, which would open it anyway
    with pytest.raises(site.PublishError) as caught:
        site.collect(out)
    assert caught.value.code == "site_unreadable:_headers"


def test_collect_refuses_a_symlinked_site_dir(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(_build(tmp_path))
    with pytest.raises(site.PublishError) as caught:
        site.collect(link)
    assert caught.value.code == "site_dir_missing"


def test_publish_uploads_only_missing_assets_and_deploys_the_manifest(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path))
    index_key = site.Asset("index.html", files["index.html"]).key
    pages = FakePages(have={index_key})
    assert site.publish(files, CONFIG, transport=pages) == "dep-1"
    uploaded = [json.loads(body)[0]["key"] for _, _, _, body in pages.calls("upload")]
    assert index_key not in uploaded and len(uploaded) == 4
    # The account token only for the project endpoints; the JWT for the asset store.
    assert pages.calls("upload-token")[0][2]["Authorization"] == f"Bearer {TOKEN}"
    for step in ("check-missing", "upload", "upsert-hashes"):
        assert all(r[2]["Authorization"] == f"Bearer {JWT}" for r in pages.calls(step))
    _, url, headers, body = pages.calls("deployments")[0]
    assert url.endswith("/accounts/acc/pages/projects/borrowrate/deployments")
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert body is not None
    manifest = json.loads(body.split(b'name="manifest"\r\n\r\n')[1].split(b"\r\n--")[0])
    assert set(manifest) == {f"/{n}" for n in site.SITE_FILES if n != "_headers"}
    assert manifest["/index.html"] == index_key
    assert b'name="_headers"; filename="_headers"\r\n\r\n<_headers>' in body


def test_the_asset_key_is_the_content_key_pages_was_shown_to_accept() -> None:
    # Measured 2026-10-23 on borrowrate: this key for this body deployed and served.
    body = b"<!doctype html><title>probe</title><p>bfx probe sha256-key"
    key = site.Asset("index.html", body).key
    assert key == hashlib.sha256((base64.b64encode(body).decode() + "html").encode()).hexdigest()[:32]
    assert key.startswith("f9eca759c4fc")  # the prefix of the key that probe deployed under


@pytest.mark.parametrize("step", ["upload-token", "check-missing", "upload", "upsert-hashes", "deployments"])
def test_any_refused_step_fails_with_its_name_and_never_the_token(tmp_path: Path, step: str) -> None:
    files = site.collect(_build(tmp_path))
    with pytest.raises(site.PublishError) as caught:
        site.publish(files, CONFIG, transport=FakePages(fail=step))
    assert caught.value.code.endswith(":http_403:10000")
    assert TOKEN not in caught.value.code and JWT not in caught.value.code


def test_verify_waits_until_production_serves_the_upload(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path))
    pages = FakePages(serves={"proof-summary.json": files["proof-summary.json"]})
    site.verify(files, CONFIG, transport=pages, sleep=lambda _: None)
    assert pages.calls("proof-summary.json")[0][1].startswith("https://borrowrate.pages.dev/")


def test_verify_fails_when_the_old_build_is_still_served(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path))
    ticks = iter(range(0, 1000, 10))
    with pytest.raises(site.PublishError) as caught:
        site.verify(files, CONFIG, transport=FakePages(serves={"proof-summary.json": b"old"}),
                    sleep=lambda _: None, clock=lambda: next(ticks), timeout=30)
    assert caught.value.code == "verify_failed:proof_summary:http_200"


def test_verify_fails_without_the_sites_csp(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path))
    pages = FakePages(serves={"proof-summary.json": files["proof-summary.json"]}, csp="")
    with pytest.raises(site.PublishError) as caught:
        site.verify(files, CONFIG, transport=pages, sleep=lambda _: None)
    assert caught.value.code == "verify_failed:index:http_200"


def _credentials(tmp_path: Path, token: str = TOKEN + "\n",
                 config: str = "BFX_PAGES_ACCOUNT_ID=acc\nBFX_PAGES_PROJECT=borrowrate\n") -> dict[str, str]:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / site.CREDENTIAL).write_text(token)
    (credentials / site.CONFIG_CREDENTIAL).write_text(config)
    return {"CREDENTIALS_DIRECTORY": str(credentials)}


def test_load_config_reads_the_units_environment_and_credential(tmp_path: Path) -> None:
    config = site.load_config(_credentials(tmp_path))
    assert config == CONFIG
    assert TOKEN not in repr(config) and TOKEN not in str(config)


@pytest.mark.parametrize("change, code", [
    (lambda env: (Path(env["CREDENTIALS_DIRECTORY"]) / site.CONFIG_CREDENTIAL).write_text(
        "BFX_PAGES_ACCOUNT_ID=acc\n"), "env_key_missing:public-site.env:BFX_PAGES_PROJECT"),
    (lambda env: env.pop("CREDENTIALS_DIRECTORY"), "credential_missing"),
    (lambda env: env.update(CREDENTIALS_DIRECTORY=env["CREDENTIALS_DIRECTORY"] + "-gone"),
     "credential_unreadable"),
])
def test_load_config_refuses_instead_of_guessing(tmp_path: Path, change, code: str) -> None:
    env = _credentials(tmp_path)
    change(env)
    with pytest.raises(site.DeployError) as caught:
        site.load_config(env)
    assert caught.value.code == code


def test_load_config_refuses_an_empty_token(tmp_path: Path) -> None:
    with pytest.raises(site.DeployError) as caught:
        site.load_config(_credentials(tmp_path, "\n"))
    assert caught.value.code == "token_empty"


def test_the_build_checks_both_files_systemd_hands_over(tmp_path: Path) -> None:
    # systemd reads them as root whatever their mode, so the build holds them to 0600.
    checked: list[Path] = []
    site.check_secret_files(tmp_path, secret_check=checked.append)
    assert checked == [tmp_path / "public-site.env", tmp_path / site.TOKEN_FILE]


def test_collect_refuses_an_oversized_file_and_a_fifo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _build(tmp_path)
    monkeypatch.setattr(site, "MAX_FILE_BYTES", 4)
    with pytest.raises(site.PublishError) as caught:
        site.collect(out)
    assert caught.value.code.startswith("site_file_too_large:")
    monkeypatch.undo()
    (out / "_headers").unlink()
    os.mkfifo(out / "_headers")
    with pytest.raises(site.PublishError) as caught:
        site.collect(out)
    assert caught.value.code == "site_not_regular:_headers"


def test_publish_never_runs_as_root_and_build_only_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site.os, "geteuid", lambda: 0)
    assert site.main(["publish"]) == 2
    monkeypatch.setattr(site.os, "geteuid", lambda: 1000)
    assert site.main(["build"]) == 2


def test_prepare_build_dir_hands_an_empty_dir_to_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(site.os, "chown", lambda path, uid, gid: owners.append((path, uid, gid)))
    build = tmp_path / "build"
    site.prepare_build_dir(build)
    assert owners == [(build, 1000, 1000)] and build.stat().st_mode & 0o777 == 0o755
    with pytest.raises(site.DeployError) as caught:  # never a previous run's directory
        site.prepare_build_dir(build)
    assert caught.value.code == "build_dir_unusable:FileExistsError"


@pytest.fixture
def build_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """The weekly job's host layout, with this file's compose file installed beside it."""
    rev, digest = "b" * 40, "sha256:" + "3" * 64
    ops = tmp_path / "releases" / rev / "ops"
    ops.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/vm/ops" / site.COMPOSE_FILE, ops / site.COMPOSE_FILE)
    (ops.parent / ".installed").write_text(rev + "\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "research.env").write_text(
        "DATABASE_URL=postgresql://bfx_research:pw@bfx-postgres:5432/bfx\n"
        "BFX_EXCHANGE_ACCOUNT_ID=00000000-0000-4000-8000-000000000001\n")
    state = tmp_path / "state"
    (state / "releases" / rev).mkdir(parents=True)
    shutil.copy(ROOT / "deploy/vm/live.env", state / "releases" / rev / "live.env")
    row = weekly.bfx_deploy.LedgerRow(id=1, attempt_id="a", source_revision=rev, backend_digest=digest,
                                      outcome="deployed", migrations_applied=False)

    class Ledger:
        def read(self) -> object:
            return weekly.bfx_deploy.LedgerView(exists=True, last_attempt=row, last_success=row)

    build_dir = tmp_path / "run" / "build"
    build_dir.parent.mkdir()
    checked: list[Path] = []
    monkeypatch.setattr(site, "_HERE", ops)
    monkeypatch.setattr(site, "BUILD_DIR", build_dir)
    monkeypatch.setattr(site.os, "geteuid", lambda: 0)
    monkeypatch.setattr(site.os, "chown", lambda *_: None)
    monkeypatch.setattr(site.bfx_deploy, "PsqlLedger", lambda *a, **k: Ledger())
    monkeypatch.setattr(site.bfx_deploy, "protected_secret_file", checked.append)
    return {"ops": ops, "runtime": runtime, "state": state, "build_dir": build_dir,
            "checked": checked, "digest": digest}


def _build_args(host: dict[str, object]) -> object:
    return site.argparse.Namespace(
        runtime_dir=host["runtime"], state_dir=host["state"],
        backend_repository="ghcr.io/will413028/bfx-funding-bot-backend",
        postgres_container="bfx-postgres", db_user="bfx", db_name="bfx")


@pytest.mark.parametrize("exit_code, code", [(0, 0), (3, 1)])
def test_build_runs_the_deployed_digest_with_the_weekly_jobs_env(
    build_host: dict[str, object], exit_code: int, code: int,
) -> None:
    ran: list[tuple[list[str], Mapping[str, str]]] = []
    result = site.run_build(_build_args(build_host),
                            execute=lambda argv, env: ran.append((argv, env)) or exit_code)
    assert result == code  # a failed build fails the unit before any upload
    argv, env = ran[0]
    assert argv == ["docker", "compose", "-p", "bfx-public-site", "-f",
                    str(build_host["ops"] / site.COMPOSE_FILE), "run", "--rm", "public-site-build"]
    assert env["BFX_BACKEND_IMAGE"].endswith("@" + str(build_host["digest"]))
    assert env["DATABASE_URL"].startswith("postgresql://bfx_research:")
    assert build_host["build_dir"].is_dir()
    # Exactly the weekly job's keys: nothing of the unit's own environment rides along.
    assert set(env) == set(weekly.bfx_deploy._base_env()) | {
        *weekly.SECRET_KEYS, *weekly.LIVE_KEYS, "BFX_BACKEND_IMAGE", "BFX_BACKEND_DIGEST",
        "BFX_SOURCE_REVISION"}
    runtime = build_host["runtime"]
    assert build_host["checked"][:2] == [runtime / "public-site.env", runtime / site.TOKEN_FILE]
    assert not any(TOKEN in value for value in env.values())


def test_build_stops_before_docker_when_a_secret_file_is_loose(
    build_host: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def loose(path: Path) -> None:
        raise site.DeployError(f"secret_file_mode:{path.name}")

    monkeypatch.setattr(site.bfx_deploy, "protected_secret_file", loose)
    assert site.run_build(_build_args(build_host),
                          execute=lambda *_: pytest.fail("built with a loose secret file")) == 1


def _configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site.os, "geteuid", lambda: 61234)  # the unit's DynamicUser
    for key, value in _credentials(tmp_path).items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("serves_new, code", [(True, 0), (False, 1)])
def test_main_exits_zero_only_once_pages_serves_the_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    serves_new: bool, code: int,
) -> None:
    _configured(tmp_path, monkeypatch)
    out = _build(tmp_path)
    pages = FakePages(serves={"proof-summary.json": (out / "proof-summary.json").read_bytes()
                              if serves_new else b"old"})
    monkeypatch.setattr(site, "urllib_transport", pages)
    monkeypatch.setattr(site, "VERIFY_SECONDS", 0)
    monkeypatch.setattr(site.time, "sleep", lambda _: None)
    assert site.main(["publish", "--site-dir", str(out)]) == code
    assert pages.calls("deployments")
    err = capsys.readouterr().err
    assert TOKEN not in err and JWT not in err


def test_every_request_names_its_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # pages.dev refuses urllib's default agent with 403; the verify step then fails a good upload.
    sent = []

    class Response:
        status = 200
        headers: Mapping[str, str] = MappingProxyType({})

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def urlopen(request, timeout):
        sent.append(request)
        return Response()

    monkeypatch.setattr(site.urllib.request, "urlopen", urlopen)
    site.urllib_transport("GET", "https://borrowrate.pages.dev/proof-summary.json", {}, None)
    assert sent[0].get_header("User-agent") == site.USER_AGENT

