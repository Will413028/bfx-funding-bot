"""Async Axiom HTTP ingest client with batching, retry, and stdout fallback.

Design basis: phase4.1-paper-shadow-infra-design.md Section "Error Handling"
- 3 retries with exp backoff (200ms / 1s / 5s)
- 401/403 immediately raise AxiomAuthError (caller exit 1)
- consecutive fail > N -> fallback: events written to stdout (Koyeb log capture)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


class AxiomAuthError(RuntimeError):
    """Axiom returned 401/403 — daemon should exit 1 (env var likely wrong)."""


class _TransientError(RuntimeError):
    """Retryable Axiom error."""


@dataclass
class AxiomConfig:
    api_key: str
    dataset: str
    base_url: str = "https://api.axiom.co"
    batch_size: int = 100
    flush_interval_s: float = 1.0
    request_timeout_s: float = 10.0


class AxiomClient:
    def __init__(self, cfg: AxiomConfig) -> None:
        self.cfg = cfg
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._http: httpx.AsyncClient | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._fallback_mode = False
        self._consecutive_fail = 0
        self._fallback_after_consecutive_fail = 3  # default; tests can override

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=self.cfg.base_url,
            timeout=self.cfg.request_timeout_s,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
        )
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await self.flush()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def emit(self, event: dict[str, Any]) -> None:
        await self._queue.put(event)

    async def flush(self) -> None:
        batch: list[dict[str, Any]] = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if not batch:
            return
        await self._send_batch(batch)

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cfg.flush_interval_s)
                if self._queue.qsize() >= self.cfg.batch_size:
                    await self.flush()
        except asyncio.CancelledError:
            raise

    async def _send_batch(self, batch: list[dict[str, Any]]) -> None:
        if self._fallback_mode:
            self._emit_stdout(batch)
            return

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.2, max=5),
                retry=retry_if_exception_type(_TransientError),
                reraise=True,
            ):
                with attempt:
                    await self._http_post(batch)
            self._consecutive_fail = 0
        except AxiomAuthError:
            raise
        except Exception as exc:
            self._consecutive_fail += 1
            log.warning("axiom_emit_fail consecutive=%d exc=%r", self._consecutive_fail, exc)
            if self._consecutive_fail >= self._fallback_after_consecutive_fail:
                self._fallback_mode = True
                log.error("axiom_fallback_mode_entered - emitting to stdout")
            self._emit_stdout(batch)

    async def _http_post(self, batch: list[dict[str, Any]]) -> None:
        assert self._http is not None
        url = f"/v1/datasets/{self.cfg.dataset}/ingest"
        try:
            resp = await self._http.post(url, json=batch)
        except httpx.RequestError as exc:
            raise _TransientError(str(exc)) from exc

        if resp.status_code in (401, 403):
            raise AxiomAuthError(f"axiom auth failed {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 500 or resp.status_code == 429:
            raise _TransientError(f"axiom {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"axiom permanent error {resp.status_code}: {resp.text[:200]}")

    @staticmethod
    def _emit_stdout(batch: list[dict[str, Any]]) -> None:
        for event in batch:
            sys.stdout.write(
                json.dumps({"axiom_fallback": True, "event": event}, default=str) + "\n"
            )
        sys.stdout.flush()
