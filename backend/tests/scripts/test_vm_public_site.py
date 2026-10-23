"""deploy/vm/ops/bfx_public_site.py -- uploading the public proof site to Cloudflare Pages.

The uploader holds the Cloudflare token and nothing else: it publishes exactly the
build the weekly container wrote, refuses a stale or unexpected one, and exits 0
only once Pages serves what it uploaded.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

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


def _build(tmp_path: Path, *, generated_at: datetime | None = None) -> Path:
    out = tmp_path / "public-site"
    generated = (generated_at or datetime.now(UTC)).isoformat()
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


def test_the_uploader_reads_the_directory_the_weekly_container_writes() -> None:
    assert site.SITE_DIR == weekly.REPORTS_DIR / "public-site"


def test_collect_returns_exactly_the_site_files(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path), now=time.time())
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
        site.collect(out, now=time.time())
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
        site.collect(out, now=time.time())
    assert caught.value.code == "site_symlink:en"


def test_collect_reports_an_unopenable_entry_as_unreadable(tmp_path: Path) -> None:
    out = _build(tmp_path)
    (out / "_headers").chmod(0)  # the suite never runs as root, which would open it anyway
    with pytest.raises(site.PublishError) as caught:
        site.collect(out, now=time.time())
    assert caught.value.code == "site_unreadable:_headers"


def test_collect_refuses_a_stale_build(tmp_path: Path) -> None:
    out = _build(tmp_path, generated_at=datetime.now(UTC) - timedelta(hours=27))
    with pytest.raises(site.PublishError) as caught:
        site.collect(out, now=time.time())
    assert caught.value.code == "site_build_stale"


def test_collect_refuses_a_symlinked_site_dir(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(_build(tmp_path))
    with pytest.raises(site.PublishError) as caught:
        site.collect(link, now=time.time())
    assert caught.value.code == "site_dir_missing"


def test_publish_uploads_only_missing_assets_and_deploys_the_manifest(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path), now=time.time())
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
    files = site.collect(_build(tmp_path), now=time.time())
    with pytest.raises(site.PublishError) as caught:
        site.publish(files, CONFIG, transport=FakePages(fail=step))
    assert caught.value.code.endswith(":http_403:10000")
    assert TOKEN not in caught.value.code and JWT not in caught.value.code


def test_verify_waits_until_production_serves_the_upload(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path), now=time.time())
    pages = FakePages(serves={"proof-summary.json": files["proof-summary.json"]})
    site.verify(files, CONFIG, transport=pages, sleep=lambda _: None)
    assert pages.calls("proof-summary.json")[0][1].startswith("https://borrowrate.pages.dev/")


def test_verify_fails_when_the_old_build_is_still_served(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path), now=time.time())
    ticks = iter(range(0, 1000, 10))
    with pytest.raises(site.PublishError) as caught:
        site.verify(files, CONFIG, transport=FakePages(serves={"proof-summary.json": b"old"}),
                    sleep=lambda _: None, clock=lambda: next(ticks), timeout=30)
    assert caught.value.code == "verify_failed:proof_summary:http_200"


def test_verify_fails_without_the_sites_csp(tmp_path: Path) -> None:
    files = site.collect(_build(tmp_path), now=time.time())
    pages = FakePages(serves={"proof-summary.json": files["proof-summary.json"]}, csp="")
    with pytest.raises(site.PublishError) as caught:
        site.verify(files, CONFIG, transport=pages, sleep=lambda _: None)
    assert caught.value.code == "verify_failed:index:http_200"


def test_load_config_reads_the_two_keys_and_the_root_only_token(tmp_path: Path) -> None:
    (tmp_path / "public-site.env").write_text("BFX_PAGES_ACCOUNT_ID=acc\nBFX_PAGES_PROJECT=borrowrate\n")
    token = tmp_path / site.TOKEN_FILE
    token.parent.mkdir(parents=True)
    token.write_text(TOKEN + "\n")
    checked: list[str] = []
    config = site.load_config(tmp_path, secret_check=lambda p: checked.append(p.name))
    assert config == CONFIG
    assert checked == ["public-site.env", "CLOUDFLARE_API_TOKEN"]
    assert TOKEN not in repr(config) and TOKEN not in str(config)


def test_load_config_refuses_a_missing_key(tmp_path: Path) -> None:
    (tmp_path / "public-site.env").write_text("BFX_PAGES_ACCOUNT_ID=acc\n")
    token = tmp_path / site.TOKEN_FILE
    token.parent.mkdir(parents=True)
    token.write_text(TOKEN)
    with pytest.raises(site.DeployError) as caught:
        site.load_config(tmp_path, secret_check=lambda _: None)
    assert caught.value.code == "env_key_missing:public-site.env:BFX_PAGES_PROJECT"


def test_collect_refuses_an_oversized_file_and_a_fifo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _build(tmp_path)
    monkeypatch.setattr(site, "MAX_FILE_BYTES", 4)
    with pytest.raises(site.PublishError) as caught:
        site.collect(out, now=time.time())
    assert caught.value.code.startswith("site_file_too_large:")
    monkeypatch.undo()
    (out / "_headers").unlink()
    os.mkfifo(out / "_headers")
    with pytest.raises(site.PublishError) as caught:
        site.collect(out, now=time.time())
    assert caught.value.code == "site_not_regular:_headers"


def test_collect_refuses_a_build_without_a_readable_build_time(tmp_path: Path) -> None:
    out = _build(tmp_path)
    (out / "proof-summary.json").write_text('{"weeks": []}')
    with pytest.raises(site.PublishError) as caught:
        site.collect(out, now=time.time())
    assert caught.value.code == "site_build_time_unreadable"


def test_main_refuses_to_run_unprivileged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site.os, "geteuid", lambda: 1000)
    assert site.main([]) == 2


def _configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(site.os, "geteuid", lambda: 0)
    monkeypatch.setattr(site.bfx_deploy, "protected_secret_file", lambda _: None)
    runtime = tmp_path / "runtime"
    (runtime / site.TOKEN_FILE).parent.mkdir(parents=True)
    (runtime / "public-site.env").write_text("BFX_PAGES_ACCOUNT_ID=acc\nBFX_PAGES_PROJECT=borrowrate\n")
    (runtime / site.TOKEN_FILE).write_text(TOKEN)
    return runtime


def test_main_dry_run_sends_nothing_and_prints_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _configured(tmp_path, monkeypatch)
    monkeypatch.setattr(site, "urllib_transport", lambda *a: pytest.fail("dry run sent a request"))
    assert site.main(["--site-dir", str(_build(tmp_path)), "--runtime-dir", str(runtime), "--dry-run"]) == 0
    out = capsys.readouterr()
    assert set(json.loads(out.out)["files"]) == set(site.SITE_FILES)
    assert TOKEN not in out.out + out.err


@pytest.mark.parametrize("serves_new, code", [(True, 0), (False, 1)])
def test_main_exits_zero_only_once_pages_serves_the_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    serves_new: bool, code: int,
) -> None:
    runtime = _configured(tmp_path, monkeypatch)
    out = _build(tmp_path)
    pages = FakePages(serves={"proof-summary.json": (out / "proof-summary.json").read_bytes()
                              if serves_new else b"old"})
    monkeypatch.setattr(site, "urllib_transport", pages)
    monkeypatch.setattr(site, "VERIFY_SECONDS", 0)
    monkeypatch.setattr(site.time, "sleep", lambda _: None)
    assert site.main(["--site-dir", str(out), "--runtime-dir", str(runtime)]) == code
    assert pages.calls("deployments")
    err = capsys.readouterr().err
    assert TOKEN not in err and JWT not in err
