"""Canonical exchange-account UUID identity."""
from __future__ import annotations

from uuid import UUID


def account_id_canonical(value: UUID | str) -> str:
    """Return the lowercase hyphenated UUID string used as credential AAD.

    Legacy realm labels and the implicit ``default`` account are intentionally
    rejected instead of being normalized into an account identity.
    """
    try:
        return str(value if isinstance(value, UUID) else UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"account identity must be a UUID, got {value!r}") from exc


