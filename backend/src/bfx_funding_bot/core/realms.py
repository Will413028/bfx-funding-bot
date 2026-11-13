"""The one list of data-isolation realms (ADR 2026-11-08 remove the shadow realm).

Settings and the ``database_realm`` CHECK derive from it. ``DeploymentEnvironment`` cannot
(core may not import modules, contract ``core-is-leaf``) and stays a literal enum so the type
checker sees its members; tests/core/test_known_realms.py holds the two equal. No imports, so
``core.settings`` and ``core.database_realm`` can both read it without a cycle.
"""

from __future__ import annotations

from typing import Final

KNOWN_REALMS: Final[tuple[str, ...]] = ("prod", "ci")
