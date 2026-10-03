"""Walk an object graph for the capital authority's legacy state.

Descends through the project's own objects, containers, bound methods and closures; any
instance whose class is a ``Legacy*`` adapter, or one of the event-sourced authority's own
state holders, is reported with the attribute path that reaches it.
"""
from __future__ import annotations

import dataclasses
import types
from collections.abc import Iterator
from typing import Any

OWN_STATE = frozenset({
    "CapitalRuntime", "CapitalRepository", "EventStorePersister", "PostgresEventStore",
    "PaperPositionLedger", "OfferRegistry", "AccountEventWriter", "BootRecovery",
})


def _is_legacy(obj: object) -> bool:
    cls = type(obj)
    return cls.__module__.startswith("bfx_funding_bot") and (
        cls.__name__.startswith("Legacy") or cls.__name__ in OWN_STATE
    )


def _children(obj: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"[{key!r}]", value
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for index, value in enumerate(obj):
            yield f"[{index}]", value
    elif isinstance(obj, types.MethodType):
        yield ".__self__", obj.__self__
    elif isinstance(obj, types.FunctionType):
        for name, cell in zip(obj.__code__.co_freevars, obj.__closure__ or (), strict=False):
            try:
                yield f"<closure {name}>", cell.cell_contents
            except ValueError:
                continue
    elif type(obj).__module__.startswith("bfx_funding_bot"):
        seen: set[str] = set()
        for cls in type(obj).__mro__:
            for name in getattr(cls, "__slots__", ()):
                if name not in seen and hasattr(obj, name):
                    seen.add(name)
                    yield f".{name}", getattr(obj, name)
        for name, value in getattr(obj, "__dict__", {}).items():
            if name not in seen:
                yield f".{name}", value
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            for field in dataclasses.fields(obj):
                if field.name not in seen and hasattr(obj, field.name):
                    yield f".{field.name}", getattr(obj, field.name)


def legacy_state(root: object, name: str = "root") -> list[str]:
    """``path: ClassName`` of every legacy adapter or event-sourced state holder under ``root``."""
    found: list[str] = []
    seen: set[int] = set()

    def visit(obj: object, path: str) -> None:
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if _is_legacy(obj):
            found.append(f"{path}: {type(obj).__name__}")
        for suffix, child in _children(obj):
            visit(child, path + suffix)

    visit(root, name)
    return found
