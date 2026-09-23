"""tenacity wrapper: retry transient (network/timeout/5xx), NOT fatal (4xx/auth)."""
from __future__ import annotations

import httpx
import pytest

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
)
from bfx_funding_bot.modules.execution.retry import (
    RETRY_ATTEMPTS,
    classify_httpx_exception,
    classify_httpx_response,
    transient_retry,
)


def _fake_response(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, request=httpx.Request("POST", "http://x"))


def test_classify_5xx_is_transient() -> None:
    err = classify_httpx_response(_fake_response(502))
    assert isinstance(err, ExecutorTransientError)


def test_classify_4xx_is_fatal() -> None:
    err = classify_httpx_response(_fake_response(400))
    assert isinstance(err, ExecutorFatalError)
    assert not isinstance(err, ExecutorTransientError)


def test_classify_401_is_auth_error() -> None:
    err = classify_httpx_response(_fake_response(401))
    assert isinstance(err, ExecutorAuthError)


def test_classify_network_error_is_transient() -> None:
    err = classify_httpx_exception(httpx.NetworkError("conn refused"))
    assert isinstance(err, ExecutorTransientError)


def test_classify_timeout_is_transient() -> None:
    err = classify_httpx_exception(httpx.TimeoutException("read timeout"))
    assert isinstance(err, ExecutorTransientError)


@pytest.mark.asyncio
async def test_transient_retry_retries_transient() -> None:
    calls = 0

    @transient_retry
    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ExecutorTransientError("flake")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_transient_retry_does_not_retry_fatal() -> None:
    calls = 0

    @transient_retry
    async def always_fatal() -> str:
        nonlocal calls
        calls += 1
        raise ExecutorAuthError("401")

    with pytest.raises(ExecutorAuthError):
        await always_fatal()
    assert calls == 1


@pytest.mark.asyncio
async def test_transient_retry_exhausts_after_n() -> None:
    calls = 0

    @transient_retry
    async def always_transient() -> str:
        nonlocal calls
        calls += 1
        raise ExecutorTransientError("persistent")

    with pytest.raises(ExecutorTransientError):
        await always_transient()
    assert calls == RETRY_ATTEMPTS
