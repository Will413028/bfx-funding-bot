"""Bitfinex credentials carried by the runtime account context."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Credentials:
    """Runtime Bitfinex credential decrypted from the account vault at boot."""
    api_key: str
    api_secret: str


