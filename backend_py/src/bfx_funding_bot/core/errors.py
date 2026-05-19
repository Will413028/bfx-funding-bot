"""Shared error taxonomy for daemon supervision.

TransientError: tenacity should retry; eventually re-raises after
stop_after_attempt cap → escalates to FatalError handling by TaskGroup.

FatalError: daemon should exit non-zero → Koyeb container restart.
Authentication, config, schema, and similar root-cause-elsewhere errors.

External modules may define their own subclasses (e.g.
AxiomTransientError(TransientError)).
"""


class TransientError(RuntimeError):
    """Retryable error. Tenacity wraps and re-invokes."""


class FatalError(RuntimeError):
    """Non-retryable error. TaskGroup cancels siblings + daemon exits."""
