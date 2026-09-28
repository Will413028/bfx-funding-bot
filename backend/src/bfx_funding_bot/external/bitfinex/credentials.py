"""Bitfinex credentials carried by the runtime account context."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Credentials:
    """Runtime Bitfinex credential decrypted from the account vault at boot."""
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

