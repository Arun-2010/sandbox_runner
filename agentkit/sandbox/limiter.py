"""Concurrency limiting with a bounded queue.

Why: each run is a container with its own memory and CPU reservation. An unbounded number of
concurrent runs turns into a host-level resource exhaustion attack, and an unbounded wait queue
just moves the problem into memory and latency. Runs beyond `max_concurrency` wait in a queue
of at most `max_queue`; past that the caller gets `SandboxBusyError` immediately (HTTP 429).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agentkit.sandbox.errors import SandboxBusyError


class ConcurrencyLimiter:
    def __init__(self, max_concurrency: int, max_queue: int) -> None:
        if max_concurrency < 1 or max_queue < 0:
            raise ValueError("max_concurrency must be >= 1 and max_queue >= 0")
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self._active = 0
        # Futures are created on the running loop at wait time (not at construction), so the
        # limiter also works across the separate event loops created by `run_sync`.
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._waiters)

    async def acquire(self) -> None:
        if self._active < self.max_concurrency and not self._waiters:
            self._active += 1
            return
        if len(self._waiters) >= self.max_queue:
            raise SandboxBusyError(f"sandbox is busy ({self._active} running, {len(self._waiters)} queued)")
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self.release()  # the slot was handed to us just before cancellation; pass it on
            else:
                self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)  # hand the slot over; _active is unchanged
                return
        self._active -= 1

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            self.release()
