"""The symbols whose history a port declared fetching, kept in the observation's evidence.

Not part of the digest and not a column: the declaration is evidence the
absence proofs (UNKNOWN matching, R6) read back per symbol. Undeclared (None)
fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping

KEY = "history_symbols"


def encode(symbols: frozenset[str] | None) -> list[str] | None:
    return None if symbols is None else sorted(symbols)


def decode(evidence: Mapping[str, object]) -> frozenset[str] | None:
    value = evidence.get(KEY)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return frozenset(value)


def declared(evidence: Mapping[str, object], symbol: str) -> bool:
    symbols = decode(evidence)
    return symbols is not None and symbol in symbols
