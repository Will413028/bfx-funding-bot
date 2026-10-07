"""An unreachable venue or database delays the boot in-process; it never crash-loops.

Before: a venue fetch failure in the boot observation raised out of
``Daemon.run`` and the process exited; the restarted process met the same
outage and exited again. Now a transient reachability failure is retried with a
capped backoff, nothing that can trade runs meanwhile, and a stop request ends
the wait cleanly. Refusals still refuse.

The backoff is injected as zero, so no test waits on a clock.

Mutation checks (one at a time; revert after each):

* re-raise every exception in ``Daemon._boot_until_observed``:
  ``test_an_unreachable_venue_at_boot_is_retried_until_it_answers``.
* drop the writer-lock check before a retry: ``test_a_lost_writer_lock_ends_the_wait``.
* treat every ``BitfinexAPIError`` 5xx as transient (ignore its ``["error", CODE, ...]``
  body): ``test_a_refusal_still_refuses_the_boot[500_10100]``.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import socket
import ssl
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import exc as sa_exc

from bfx_funding_bot.core.errors import (
    BootInvariantError,
    ExecutorAuthError,
    ExecutorTransientError,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.execution.safety.protection import WriterLockLostError
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.marketfeed.boot_wait import (
    boot_retry_delay_s,
    is_transient_dependency_error,
    wait_for_database,
)
from bfx_funding_bot.modules.marketfeed.daemon import Daemon


class _Venue:
    """The boot observation: unreachable for the first ``failures`` calls."""

    def __init__(self, failures: int, error: Callable[[], BaseException]) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    async def run(self, scope: Scope) -> CycleResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error()
        return CycleResult("accepted", uuid4())


def _unreachable() -> BaseException:
    return BitfinexAPIError(status_code=0, message="transport error: connect failed")


def _daemon(venue: _Venue, *, delay: Callable[[int], float] = lambda _n: 0.0,
            lock_watch: Any = None) -> Any:
    executor = MagicMock()
    executor.submit = AsyncMock()
    fake = SimpleNamespace(
        boot_recovery=venue, observation_scope=Scope(uuid4(), "ci"),
        protection=SimpleNamespace(run_pending=AsyncMock()), periodic_reconcile=None,
        writer_lock_watch=lock_watch, boot_retry_delay_s=delay,
        _stop_event=asyncio.Event(), booted=False,
        config=SimpleNamespace(cells=[]), scheduler=MagicMock(), executor=executor,
    )
    fake._run_boot_recovery = lambda: Daemon._run_boot_recovery(fake)  # type: ignore[arg-type]
    fake._boot_until_observed = lambda: Daemon._boot_until_observed(fake)  # type: ignore[arg-type]
    return fake


async def test_an_unreachable_venue_at_boot_is_retried_until_it_answers(caplog) -> None:
    lock_watch = SimpleNamespace(check=AsyncMock(return_value=True))
    venue = _Venue(3, _unreachable)
    fake = _daemon(venue, lock_watch=lock_watch)

    with caplog.at_level(logging.WARNING):
        assert await Daemon._boot_until_observed(fake) is True

    assert venue.calls == 4
    # The observation only reruns as the single writer.
    assert lock_watch.check.await_count == 3
    assert caplog.text.count("boot_waiting_for_dependency step=venue observation") == 3
    # One operator alert per wait, not per attempt.
    assert caplog.text.count("boot waiting for an unreachable venue observation") == 1


async def test_nothing_trades_while_the_boot_waits_and_a_stop_ends_the_wait() -> None:
    venue = _Venue(10**6, _unreachable)
    stop_after = 3

    def delay(attempt: int) -> float:
        if attempt == stop_after:
            fake._stop_event.set()
        return 0.0

    fake = _daemon(venue, delay=delay)
    # A SimpleNamespace daemon has none of the sub-task wiring: reaching the
    # TaskGroup (anything that could trade) would raise AttributeError here.
    await Daemon.run(fake)

    assert venue.calls == stop_after
    assert fake.booted is False
    fake.scheduler.start.assert_not_called()
    fake.executor.submit.assert_not_awaited()


@pytest.mark.parametrize(
    ("label", "error"),
    [
        # Bitfinex refuses a revoked or wrong key with HTTP 500 and a parsed body.
        ("500_10100", lambda: BitfinexAPIError(status_code=500, message="Internal Server Error",
                                               raw='["error",10100,"apikey: invalid"]')),
        ("auth", lambda: ExecutorAuthError("auth_failed: HTTP 401")),
        ("invariant", lambda: BootInvariantError("boot observation refused query admission")),
        ("value", lambda: ValueError("boot observation scope is required")),
    ],
)
async def test_a_refusal_still_refuses_the_boot(label: str, error: Callable[[], BaseException]) -> None:
    venue = _Venue(1, error)
    fake = _daemon(venue)

    with pytest.raises(type(error())):
        await Daemon._boot_until_observed(fake)
    assert venue.calls == 1


async def test_a_lost_writer_lock_ends_the_wait() -> None:
    lock_watch = SimpleNamespace(
        check=AsyncMock(side_effect=WriterLockLostError("writer lock not held after refresh")),
    )
    venue = _Venue(1, _unreachable)
    fake = _daemon(venue, lock_watch=lock_watch)

    with pytest.raises(WriterLockLostError):
        await Daemon._boot_until_observed(fake)
    assert venue.calls == 1


# ── classification ────────────────────────────────────────────────────────────


def _caused(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("refused"),
        TimeoutError("deadline"),
        httpx.ConnectError("no route"),
        BitfinexAPIError(status_code=0, message="transport error"),
        BitfinexAPIError(status_code=429, message="rate limited"),
        BitfinexAPIError(status_code=503, message="maintenance"),
        # A gateway page or empty body: no venue answer to read.
        BitfinexAPIError(status_code=502, message="Bad Gateway", raw="<html>bad gateway</html>"),
        BitfinexAPIError(status_code=500, message="Internal Server Error", raw=""),
        # The venue's own "not now": rate limit and maintenance codes.
        BitfinexAPIError(status_code=500, message="x", raw='["error",11010,"ratelimit: error"]'),
        BitfinexAPIError(status_code=500, message="x", raw='["error",20060,"maintenance"]'),
        socket.gaierror(-2, "Name or service not known"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError("Multiple exceptions: [Errno 61] refused, [Errno 61] refused"),
        ExecutorTransientError("venue_5xx: HTTP 502"),
        _caused(RuntimeError("cycle failed"), OSError("socket closed")),
        sa_exc.DBAPIError("SELECT 1", None, Exception("gone"), connection_invalidated=True),
    ],
)
def test_reachability_faults_are_transient(exc: BaseException) -> None:
    assert is_transient_dependency_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        BitfinexAPIError(status_code=401, message="invalid key"),
        BitfinexAPIError(status_code=400, message="bad request"),
        BitfinexAPIError(status_code=500, message="x", raw='["error",10100,"apikey: invalid"]'),
        BitfinexAPIError(status_code=500, message="x", raw='["error",10114,"nonce: small"]'),
        # This process's own setup, not the other side being away.
        ssl.SSLCertVerificationError(1, "certificate verify failed"),
        PermissionError(13, "Permission denied"),
        # Local resource and file errors are OSErrors too, but not the other side being away.
        FileNotFoundError(2, "No such file or directory"),
        IsADirectoryError(21, "Is a directory"),
        OSError(errno.EMFILE, "Too many open files"),
        ValueError("unknown active offer status"),
        sa_exc.DBAPIError("SELECT 1", None, Exception("syntax"), connection_invalidated=False),
        # A refusal caused by a network error stays a refusal.
        _caused(BootInvariantError("refused"), OSError("socket closed")),
    ],
)
def test_answers_and_refusals_are_not_transient(exc: BaseException) -> None:
    assert is_transient_dependency_error(exc) is False


def test_a_refusal_raised_while_handling_a_network_error_is_not_transient() -> None:
    try:
        try:
            raise OSError("socket closed")
        except OSError:
            raise ValueError("refused") from None
    except ValueError as exc:
        assert is_transient_dependency_error(exc) is False


def test_the_backoff_doubles_up_to_a_minute() -> None:
    assert [boot_retry_delay_s(n) for n in (1, 2, 3, 6, 7, 50)] == [1, 2, 4, 32, 60, 60]


# ── database ─────────────────────────────────────────────────────────────────


class _Engine:
    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        self.attempts = 0

    def connect(self) -> Any:
        engine = self

        class _Conn:
            async def __aenter__(self) -> Any:
                engine.attempts += 1
                if engine.errors:
                    raise engine.errors.pop(0)
                return SimpleNamespace(execute=AsyncMock())

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Conn()


async def test_boot_waits_for_a_database_that_is_down_or_starting() -> None:
    engine = _Engine([ConnectionRefusedError("refused"), OSError("name not known")])
    await wait_for_database(engine, delay_s=lambda _n: 0.0)  # type: ignore[arg-type]
    assert engine.attempts == 3


async def test_a_database_that_answers_with_an_error_refuses_the_boot() -> None:
    engine = _Engine([RuntimeError("password authentication failed")])
    with pytest.raises(RuntimeError, match="password"):
        await wait_for_database(engine, delay_s=lambda _n: 0.0)  # type: ignore[arg-type]
    assert engine.attempts == 1


@pytest.mark.parametrize("error", [
    ssl.SSLCertVerificationError(1, "certificate verify failed"),
    PermissionError(13, "Permission denied"),
])
async def test_a_database_tls_or_permission_error_refuses_the_boot(error: OSError) -> None:
    engine = _Engine([error])
    with pytest.raises(type(error)):
        await wait_for_database(engine, delay_s=lambda _n: 0.0)  # type: ignore[arg-type]
    assert engine.attempts == 1


# ── a TLS certificate the client does not trust, through the real clients ─────


def _self_signed(directory: Path) -> tuple[Path, Path]:
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


@pytest.fixture
async def untrusted_tls_url(tmp_path: Path) -> Any:
    """A local HTTPS server whose self-signed certificate no client trusts."""
    cert_path, key_path = _self_signed(tmp_path)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_path, key_path)

    async def close(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(close, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"https://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


def _has_cert_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    return False


async def test_an_untrusted_certificate_through_httpx_refuses_the_boot(untrusted_tls_url) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.ConnectError) as caught:
            await http.get(untrusted_tls_url)
    assert _has_cert_failure(caught.value)  # the production shape: ConnectError over the TLS error
    assert is_transient_dependency_error(caught.value) is False


async def test_an_untrusted_certificate_through_the_public_rest_client_refuses_the_boot(
    untrusted_tls_url,
) -> None:
    from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
    from bfx_funding_bot.external.bitfinex.rest import BitfinexREST

    async with httpx.AsyncClient() as http:
        rest = BitfinexREST(http=http, base_url=untrusted_tls_url, limiter=FundingRateLimiter())
        with pytest.raises(BitfinexAPIError) as caught:
            await rest.get_funding_book(symbol="fUST")
    assert caught.value.status_code == 0 and _has_cert_failure(caught.value)
    assert is_transient_dependency_error(caught.value) is False


async def test_an_untrusted_certificate_through_the_auth_rest_client_ends_the_boot_wait(
    untrusted_tls_url,
) -> None:
    """The boot observation's own path: the signed read wraps the TLS failure as
    status 0 ("no answer"); the boot refuses at the first attempt, it does not wait."""
    from decimal import Decimal

    from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
    from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

    ctx = AccountContext("acct", Credentials("k" * 43, "s" * 43), Decimal("0"))
    async with httpx.AsyncClient() as http:
        auth_rest = BitfinexAuthREST(http=http, base_url=untrusted_tls_url)

        class _Observation:
            calls = 0

            async def run(self, scope: Scope) -> CycleResult:
                _Observation.calls += 1
                await auth_rest.fetch_wallet_observations(ctx=ctx)
                raise AssertionError("unreachable")

        def no_retry(attempt: int) -> float:
            raise AssertionError(f"the boot waited on a TLS refusal (attempt {attempt})")

        fake = _daemon(_Venue(0, _unreachable), delay=no_retry)
        fake.boot_recovery = _Observation()
        with pytest.raises(BitfinexAPIError) as caught:
            await Daemon._boot_until_observed(fake)
    assert caught.value.status_code == 0 and _has_cert_failure(caught.value)
    assert _Observation.calls == 1
