"""Shared immutable data carriers. Archive validation belongs to its consumer."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Scope:
    account_id: UUID
    environment: str


@dataclass(frozen=True)
class StreamIdentity:
    count: int
    head: int
    digest: str


@dataclass(frozen=True)
class Difference:
    table: str
    key_digest: str
    column: str
    before_digest: str | None
    after_digest: str | None
    classification: str = "unexplained"


@dataclass(frozen=True)
class ArchiveManifest:
    run_id: UUID
    scope: Scope
    stream: StreamIdentity
    format_version: int
    image_digest: str
    migration_heads: tuple[str, ...]
    projector_version: str
    tables: tuple[dict[str, object], ...]
    digest: str
