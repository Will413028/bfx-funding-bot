"""Shared error taxonomy for daemon supervision.

TransientError: tenacity should retry; eventually re-raises after
stop_after_attempt cap → escalates to FatalError handling by TaskGroup.

FatalError: daemon should exit non-zero → Koyeb container restart.
Authentication, config, schema, and similar root-cause-elsewhere errors.
"""


class TransientError(RuntimeError):
    """Retryable error. Tenacity wraps and re-invokes."""


class FatalError(RuntimeError):
    """Non-retryable error. TaskGroup cancels siblings + daemon exits."""


class ExecutorTransientError(TransientError):
    """Network / 5xx from venue. tenacity retries within executor scope."""


class ExecutorFatalError(FatalError):
    """4xx / logical errors from venue. Propagates to daemon TaskGroup."""


class ExecutorAuthError(ExecutorFatalError):
    """Credentials rejected by venue. Needs operator — NOT auto-retry.

    daemon._run catches this distinctly → flush + sys.exit(EXIT_CODE_AUTH_FAILED)
    so Koyeb restart policy enters crash-loop-backoff instead of tight retry.
    See spec Path E + Google SRE Book ch. 22.
    """


# Exit codes (sysexits.h)
EXIT_CODE_AUTH_FAILED = 78  # EX_CONFIG
