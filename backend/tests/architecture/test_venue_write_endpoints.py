"""The bot's authenticated venue writes are exactly the three funding-offer endpoints (F31).

The real defence is the key itself: it may write funding and nothing else
(``modules.accounts.vault.permission_error``, rechecked at boot and every 6 hours). This test
stops the code drifting toward more: a new ``auth/w/`` path, a request signed outside the two
signing clients, or an order input sent on the authenticated WebSocket fails here first.
The simulated venue (``modules/simulated_venue``) is the venue side and is not scanned.

Mutation checks (one at a time; revert after each):

* add ``_ORDER_SUBMIT_PATH = "v2/auth/w/order/submit"`` to ``live_executor.py``:
  ``test_the_only_authenticated_writes_are_the_three_funding_offer_endpoints`` fails.
* call ``sign_request(...)`` from another module (or import it there under another name):
  ``test_only_the_executor_and_the_read_client_sign_requests`` fails.
* point an ``auth_rest.py`` path constant at ``v2/auth/w/...``: both of the above fail.
* send ``json.dumps([0, "fon", None, {...}])`` on the authenticated WebSocket:
  ``test_the_authenticated_websocket_sends_only_its_handshake`` fails.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bfx_funding_bot"
VENUE_SIDE = "modules/simulated_venue/"
EXECUTOR = "external/bitfinex/live_executor.py"
READ_CLIENT = "external/bitfinex/auth_rest.py"
SIGNER = "external/bitfinex/auth_ws.py"
WRITES = {
    "v2/auth/w/funding/offer/submit",
    "v2/auth/w/funding/offer/cancel",
    "v2/auth/w/funding/offer/cancel/all",
}


def _modules() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if not rel.startswith(VENUE_SIDE):
            out.append((rel, ast.parse(path.read_text(), filename=rel)))
    return out


def _docstrings(tree: ast.Module) -> set[int]:
    """ids of the docstring constants: prose may name an endpoint, code may not."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _strings(tree: ast.Module) -> list[str]:
    skip = _docstrings(tree)
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in skip]


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_the_only_authenticated_writes_are_the_three_funding_offer_endpoints() -> None:
    found = sorted(
        (rel, value) for rel, tree in _modules() for value in _strings(tree)
        if "auth/w/" in value
    )
    assert found == sorted((EXECUTOR, path) for path in WRITES), (
        "a new authenticated venue write: the key may only write funding offers (F31); "
        "add it here only with the permission rule it needs")


def test_only_the_executor_and_the_read_client_sign_requests() -> None:
    importers: set[str] = set()
    callers: dict[str, int] = {}
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "sign_request" for alias in node.names):
                importers.add(rel)
            if isinstance(node, ast.Call) and _called_name(node) == "sign_request":
                callers[rel] = callers.get(rel, 0) + 1
    assert importers == {EXECUTOR, READ_CLIENT}
    # The executor signs its three writes; the read client signs every read through one
    # ``_signed_post``.
    assert callers == {EXECUTOR: 3, READ_CLIENT: 1}


def test_the_read_client_signs_reads_only() -> None:
    tree = ast.parse((SRC / READ_CLIENT).read_text())
    paths = [value for value in _strings(tree) if value.startswith("v2/auth/")]
    assert paths, "the read client's path constants moved: update this test"
    assert [p for p in paths if not p.startswith("v2/auth/r/")] == []


def test_the_authenticated_websocket_sends_only_its_handshake() -> None:
    """The auth WebSocket could carry order inputs (``on``, ``fon``, ``foc``...); this bot only
    reads it. Every ``send`` there is the auth frame or the ``conf`` frame."""
    tree = ast.parse((SRC / SIGNER).read_text())
    sent = sorted(ast.unparse(node.args[0]) for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and _called_name(node) == "send" and node.args)
    assert sent == ["json.dumps(auth)", "json.dumps({'event': 'conf', 'flags': SEQ_ALL_FLAG})"]
    # The handshake's own signature is built in this one module.
    builders = {rel for rel, tree in _modules() for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _called_name(node) == "build_auth_payload"}
    assert builders == {SIGNER}
