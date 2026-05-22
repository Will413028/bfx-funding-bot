"""Execution-domain error taxonomy.

Per Phase 4.4a spec §8.1: every error in modules/execution + external/bitfinex
inherits from these four. Forbids string-match dispatch.

  - TransientError: network / timeout / rate-limit. Retry-safe under idempotency.
  - PermanentError: business reject (bad params / insufficient balance). No retry.
  - InvariantViolation: internal state inconsistency. Fail-fast → daemon cancel.
  - AuthError: credential / signature failure. Boot fail-fast; runtime 5-retry escalate.
"""
from __future__ import annotations


class ExecutionError(Exception):
    """Base class for all execution-domain errors."""


class TransientError(ExecutionError):
    """Network / timeout / rate-limit. Retry-safe."""


class PermanentError(ExecutionError):
    """Business reject. No retry."""


class InvariantViolation(ExecutionError):  # noqa: N818
    """Internal state inconsistency. Fail-fast."""


class AuthError(ExecutionError):
    """Credential / signature failure."""
