import pytest

from bfx_funding_bot.modules.execution.errors import (
    AuthError,
    ExecutionError,
    InvariantViolation,
    PermanentError,
    TransientError,
)


def test_taxonomy_inheritance():
    assert issubclass(TransientError, ExecutionError)
    assert issubclass(PermanentError, ExecutionError)
    assert issubclass(InvariantViolation, ExecutionError)
    assert issubclass(AuthError, ExecutionError)


def test_each_class_distinct():
    classes = {TransientError, PermanentError, InvariantViolation, AuthError}
    assert len(classes) == 4


def test_raise_with_message():
    with pytest.raises(TransientError, match="upstream timeout"):
        raise TransientError("upstream timeout")
