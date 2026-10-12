"""Every operator alert is registered and has a runbook entry.

- Every ``alerts.emit(...)`` in src/ and scripts/ names its event as an
  ``alerts.<CONSTANT>`` registered in ``alerts.ALERT_EVENTS`` (a string literal
  or a local constant would slip past the registry), and every registered event
  is emitted somewhere.
- Every registered event has the anchor ``<a id="alert-<event>"></a>`` in
  docs/runbooks/operations.md, except the few in NO_RUNBOOK; stale anchors fail.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from bfx_funding_bot.modules.observability import alerts

BACKEND = Path(__file__).resolve().parents[2]
SOURCES = (BACKEND / "src", BACKEND / "scripts")
RUNBOOK = BACKEND.parent / "docs" / "runbooks" / "operations.md"
ANCHOR = re.compile(r'<a id="alert-([a-z0-9_]+)"></a>')

# Events that need no runbook section (none today). Lower NO_RUNBOOK_MAX when an
# entry gets a section; raising it needs a reason in the commit message.
NO_RUNBOOK: frozenset[str] = frozenset()
NO_RUNBOOK_MAX = 0


def _emit_calls() -> Iterator[tuple[str, ast.Call]]:
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "emit" and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "alerts"):
                    yield f"{path.relative_to(BACKEND)}:{node.lineno}", node


def _event_names(arg: ast.expr) -> list[str] | None:
    """The alerts.<CONSTANT> names an event argument can evaluate to, or None."""
    if isinstance(arg, ast.IfExp):
        body, orelse = _event_names(arg.body), _event_names(arg.orelse)
        return None if body is None or orelse is None else body + orelse
    if (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
            and arg.value.id == "alerts"):
        return [arg.attr]
    return None


def _emitted() -> dict[str, list[str]]:
    """Event value -> call sites; fails on any argument that is not alerts.<CONSTANT>."""
    found: dict[str, list[str]] = {}
    bad: list[str] = []
    for where, call in _emit_calls():
        names = _event_names(call.args[0]) if call.args else None
        if names is None:
            bad.append(f"{where}: {ast.unparse(call.args[0]) if call.args else '<none>'}")
            continue
        for name in names:
            value = getattr(alerts, name, None)
            if not isinstance(value, str) or value not in alerts.ALERT_EVENTS:
                bad.append(f"{where}: alerts.{name}")
                continue
            found.setdefault(value, []).append(where)
    assert not bad, ("alerts.emit must name an alerts.<CONSTANT> registered in "
                     f"alerts.ALERT_EVENTS: {bad}")
    return found


def _hides_emit(node: ast.AST) -> bool:
    """A form the scan above cannot see: the module under another name, names
    imported out of it, or a call through the installed sink."""
    if isinstance(node, ast.Import):
        return any(a.name.endswith("observability.alerts") for a in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module.endswith("observability.alerts") or (node.level and module == "alerts"):
            return True
        if module.endswith("observability") or (node.level and module == ""):
            return any(a.name == "alerts" and a.asname not in (None, "alerts")
                       for a in node.names)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit" and isinstance(node.func.value, ast.Call)):
        inner = node.func.value.func
        return isinstance(inner, ast.Attribute) and inner.attr == "current"
    return False


def test_alerts_module_is_only_reached_as_alerts() -> None:
    """Every alert goes through ``alerts.emit`` with the module bound as ``alerts``."""
    alerts_py = Path(alerts.__file__).resolve()
    offenders = [
        f"{path.relative_to(BACKEND)}:{node.lineno}"
        for root in SOURCES
        for path in sorted(root.rglob("*.py")) if path.resolve() != alerts_py
        for node in ast.walk(ast.parse(path.read_text()))
        if _hides_emit(node)
    ]
    assert offenders == []


def test_every_emit_names_a_registered_event() -> None:
    found = _emitted()
    assert sum(len(sites) for sites in found.values()) >= 30, found  # the scan saw the calls


def test_every_registered_event_is_emitted() -> None:
    assert sorted(set(alerts.ALERT_EVENTS) - set(_emitted())) == []


def test_registered_levels_are_known() -> None:
    assert set(alerts.ALERT_EVENTS.values()) <= {alerts.INFO, alerts.WARNING, alerts.CRITICAL}


def test_every_registered_event_has_a_runbook_anchor() -> None:
    anchors = set(ANCHOR.findall(RUNBOOK.read_text()))
    missing = sorted(set(alerts.ALERT_EVENTS) - NO_RUNBOOK - anchors)
    assert missing == [], f"add <a id=\"alert-<event>\"></a> sections to {RUNBOOK.name}: {missing}"
    stale = sorted(anchors - set(alerts.ALERT_EVENTS))
    assert stale == [], f"runbook anchors for events that are not registered: {stale}"


def test_no_runbook_list_is_locked() -> None:
    assert set(alerts.ALERT_EVENTS) >= NO_RUNBOOK
    assert len(NO_RUNBOOK) == NO_RUNBOOK_MAX
