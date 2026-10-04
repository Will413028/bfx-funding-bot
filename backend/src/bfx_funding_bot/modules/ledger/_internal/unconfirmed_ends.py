"""Offers whose end the port could not date, kept in the observation's evidence.

Like ``history_symbols`` this is a declaration of the port, stored as evidence of the
accepted observation (not a digest input, not a column) and read back by the basis, so a
basis is a function of stored rows alone. Missing or malformed fails closed: no id is
unconfirmed, and an absent offer without a terminal row keeps its conflict.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

KEY = "unconfirmed_offer_ends"


def encode(ids: Collection[str]) -> list[str]:
    return sorted(ids)


def decode(evidence: Mapping[str, object]) -> frozenset[str]:
    value = evidence.get(KEY)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return frozenset()
    return frozenset(value)
