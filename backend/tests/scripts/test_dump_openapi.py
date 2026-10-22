"""The committed OpenAPI document is the app's, and it types every /api/v1 response (9k)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import dump_openapi


def test_the_committed_document_is_current() -> None:
    assert dump_openapi.OPENAPI_JSON.read_text() == dump_openapi.render(), (
        "backend/openapi.json is stale: run `uv run python scripts/dump_openapi.py`, "
        "then `pnpm gen:api` in frontend/")


def test_every_api_json_response_names_a_schema() -> None:
    """No /api/v1 JSON route answers an untyped object (what a ``-> dict`` handler gives)."""
    document = json.loads(dump_openapi.render())
    schemas = document["components"]["schemas"]
    untyped = []
    for path, item in document["paths"].items():
        for method, operation in item.items():
            for code, response in operation["responses"].items():
                if not path.startswith("/api/v1") or not code.startswith("2") or code == "204":
                    continue
                if path.endswith(".csv"):
                    continue
                ref = response["content"]["application/json"]["schema"].get("$ref", "")
                name = ref.removeprefix("#/components/schemas/")
                if not ref or "properties" not in schemas[name]:
                    untyped.append(f"{method.upper()} {path} {code}")
    assert untyped == []


def test_check_fails_on_a_stale_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "openapi.json"
    monkeypatch.setattr(dump_openapi, "OPENAPI_JSON", target)
    assert dump_openapi.main(["--check"]) == 1
    assert dump_openapi.main([]) == 0
    assert dump_openapi.main(["--check"]) == 0
    target.write_text(target.read_text().replace('"userId"', '"user_id"', 1))
    assert dump_openapi.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err
