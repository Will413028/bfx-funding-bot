"""Phase 4.2 executor error taxonomy + exit codes."""
from bfx_funding_bot.core.errors import (
    EXIT_CODE_AUTH_FAILED,
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
    FatalError,
    TransientError,
)


def test_executor_transient_is_transient() -> None:
    assert issubclass(ExecutorTransientError, TransientError)


def test_executor_fatal_is_fatal() -> None:
    assert issubclass(ExecutorFatalError, FatalError)


def test_executor_auth_is_fatal_subclass() -> None:
    assert issubclass(ExecutorAuthError, ExecutorFatalError)


def test_exit_code_auth_failed_is_sysexits_78() -> None:
    # sysexits.h EX_CONFIG = 78
    assert EXIT_CODE_AUTH_FAILED == 78
