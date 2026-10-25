"""The bot's authenticated venue writes are exactly the three funding-offer endpoints (F31).

The real defence is the key itself: it may write funding and nothing else
(``modules.credentials.vault.permission_error``, rechecked at boot and every 6 hours). This test
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
* let :func:`auth_path_violations` skip ``+``-assembled pieces in a signing client:
  ``test_an_assembled_authenticated_path_is_caught[read_client_concat]`` fails (the other
  cases are caught by the whole-literal rules as well).
* let :func:`signing_modules` ignore ``hmac.new`` or ``sha384``:
  ``test_a_hand_rolled_signature_is_caught[*]`` fails.

A path is checked as the string pieces the source holds: any piece naming ``auth/`` outside
the two signing clients, and in them any piece joined with ``+``, ``%`` or an f-string instead
of written whole. A signature is checked by its primitives: ``hmac`` beyond
``compare_digest`` and SHA-384 (Bitfinex's signing hash) appear only in the signing module.
This is a tripwire against an accidental new write, not a proof: a path split so that no
piece names ``auth/`` (``"/".join([...])``, ``str.format``, an interpolated constant), a
signature built with ``cryptography``'s HMAC, or a direct call of ``auth_rest._signed_post``
from another module is not caught; review still owns those.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

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


def _assembled(tree: ast.Module) -> set[int]:
    """ids of the string pieces joined into a larger string: an f-string's literal parts and
    the operands of ``+`` / ``%``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            ids.update(id(part) for part in node.values)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
            ids.update(id(side) for side in (node.left, node.right))
    return ids


def auth_path_violations(modules: list[tuple[str, ast.Module]]) -> list[str]:
    """Every string piece that names an authenticated venue path (``auth/``) where it may not:
    outside the executor and the read client; there, a piece joined into a larger string (a
    path assembled at run time escapes the lists below); in the executor, anything but one of
    :data:`WRITES`; in the read client, anything but a ``v2/auth/r/`` read."""
    bad = []
    for rel, tree in modules:
        joined = _assembled(tree)
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "auth/" in node.value and id(node) not in skip):
                continue
            value = node.value
            if rel not in (EXECUTOR, READ_CLIENT):
                bad.append(f"{rel}: {value!r} outside the signing clients")
            elif id(node) in joined:
                bad.append(f"{rel}: {value!r} is assembled, not written whole")
            elif rel == EXECUTOR and value not in WRITES:
                bad.append(f"{rel}: {value!r} is not a funding offer write")
            elif rel == READ_CLIENT and not value.startswith("v2/auth/r/"):
                bad.append(f"{rel}: {value!r} is not a read")
    return bad


def signing_modules(modules: list[tuple[str, ast.Module]]) -> set[str]:
    """The modules holding a signing primitive: ``hmac`` used for anything but
    ``compare_digest`` (``hmac.new``, ``hmac.HMAC``, ``hmac.digest``, a ``from hmac import``
    of them, ``import hmac as ...``) or SHA-384 in any spelling (``hashlib.sha384``, an
    imported ``sha384``, ``hashlib.new("sha384")``)."""
    found = set()
    for rel, tree in modules:
        for node in ast.walk(tree):
            if ((isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                 and node.value.id == "hmac" and node.attr != "compare_digest")
                    or (isinstance(node, ast.ImportFrom) and node.module == "hmac"
                        and any(a.name != "compare_digest" for a in node.names))
                    or (isinstance(node, ast.Import)
                        and any(a.name == "hmac" and a.asname for a in node.names))
                    or (isinstance(node, ast.Attribute) and node.attr == "sha384")
                    or (isinstance(node, ast.Name) and node.id == "sha384")
                    or (isinstance(node, ast.alias) and node.name == "sha384")
                    or (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value.lower().replace("-", "") == "sha384")):
                found.add(rel)
    return found


def test_the_only_authenticated_writes_are_the_three_funding_offer_endpoints() -> None:
    modules = _modules()
    assert auth_path_violations(modules) == [], (
        "a new authenticated venue write: the key may only write funding offers (F31); "
        "add it here only with the permission rule it needs")
    writes = sorted(value for rel, tree in modules if rel == EXECUTOR
                    for value in _strings(tree) if "auth/" in value)
    assert writes == sorted(WRITES)


def test_only_the_signing_module_holds_a_signing_primitive() -> None:
    assert signing_modules(_modules()) == {SIGNER}


def _snippet(source: str, rel: str = "modules/execution/elsewhere.py") -> list[tuple[str, ast.Module]]:
    return [(rel, ast.parse(source))]


@pytest.mark.parametrize(("rel", "source"), [
    ("modules/execution/elsewhere.py", 'P = "v2/auth/" + "w/order/submit"'),
    ("modules/execution/elsewhere.py", 'P = f"v2/auth/{kind}/order/submit"'),
    ("modules/execution/elsewhere.py", 'P = "v2/auth/%s" % "w/order/submit"'),
    (EXECUTOR, 'P = "v2/auth/" + "w/order/submit"'),
    (EXECUTOR, 'P = f"v2/auth/w/{kind}/submit"'),
    (READ_CLIENT, 'P = "v2/auth/r/" + "../w/order/submit"'),
], ids=["concat", "fstring", "percent", "executor_concat", "executor_fstring",
        "read_client_concat"])
def test_an_assembled_authenticated_path_is_caught(rel: str, source: str) -> None:
    assert auth_path_violations(_snippet(source, rel)) != []


def test_a_written_whole_funding_write_in_the_executor_passes() -> None:
    assert auth_path_violations(_snippet('P = "v2/auth/w/funding/offer/submit"', EXECUTOR)) == []


@pytest.mark.parametrize("source", [
    "import hmac, hashlib\nsig = hmac.new(k, m, hashlib.sha384).hexdigest()",
    "import hmac\nsig = hmac.HMAC(k, m, 'sha384').hexdigest()",
    "import hmac\nsig = hmac.digest(k, m, 'sha384')",
    "from hmac import new\nsig = new(k, m, digestmod).hexdigest()",
    "import hmac as h\nsig = h.new(k, m, d).hexdigest()",
    "from hashlib import sha384\nd = sha384(m).hexdigest()",
    "import hashlib\nd = hashlib.new('sha384', m).hexdigest()",
], ids=["hmac_new", "hmac_class", "hmac_digest", "from_hmac", "hmac_alias",
        "sha384_import", "hashlib_new"])
def test_a_hand_rolled_signature_is_caught(source: str) -> None:
    assert signing_modules(_snippet(source)) == {"modules/execution/elsewhere.py"}


def test_comparing_digests_is_not_signing() -> None:
    source = "import hmac\nok = hmac.compare_digest(a, b)"
    assert signing_modules(_snippet(source)) == set()


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
