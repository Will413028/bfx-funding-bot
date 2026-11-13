"""Bitfinex credentials carried by the runtime account context."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Credentials:
    """Runtime Bitfinex credential decrypted from the account vault at boot."""
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


class HasCredentials(Protocol):
    """Whatever signs a request: the venue reads nothing else of the account."""

    @property
    def credentials(self) -> Credentials: ...
