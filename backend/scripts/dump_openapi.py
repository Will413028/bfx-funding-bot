"""Write the web API's OpenAPI document, the source of the frontend's API types (9k).

The document is built offline from the app object (``app.openapi()``); production serves
no schema (``openapi_url=None``). ``backend/openapi.json`` is committed, and the frontend
generates ``src/types/api.gen.ts`` from it (``pnpm gen:api``). CI regenerates both and fails
on any difference, so a response model change lands together with the types it changes.

Usage:
    cd backend
    uv run python scripts/dump_openapi.py            # rewrite openapi.json
    uv run python scripts/dump_openapi.py --check    # exit 1 if openapi.json is stale

Exit codes: 0 = written, or current under --check; 1 = --check found it stale.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OPENAPI_JSON = Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    """The document as committed: two-space JSON in the app's route order, newline-terminated."""
    from bfx_funding_bot.apps.webapi import app

    return json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if openapi.json is stale")
    args = parser.parse_args(argv)
    document = render()
    if args.check:
        current = OPENAPI_JSON.read_text() if OPENAPI_JSON.exists() else ""
        if current != document:
            print(f"{OPENAPI_JSON.name} is stale: run `uv run python scripts/dump_openapi.py`, "
                  "then `pnpm gen:api` in frontend/", file=sys.stderr)
            return 1
        return 0
    OPENAPI_JSON.write_text(document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
