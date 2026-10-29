from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agentkit.sandbox.errors import SandboxBusyError
from agentkit.sandbox.janitor import parse_docker_timestamp
from agentkit.sandbox.limiter import ConcurrencyLimiter


async def test_concurrency_respected() -> None:
    limiter = ConcurrencyLimiter(max_concurrency=2, max_queue=10)
    running = peak = 0

    async def job() -> None:
        nonlocal running, peak
        async with limiter.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

    await asyncio.gather(*(job() for _ in range(8)))
    assert peak == 2 and limiter.active == 0 and limiter.queued == 0


async def test_queue_overflow_raises_busy() -> None:
    limiter = ConcurrencyLimiter(max_concurrency=1, max_queue=1)
    release = asyncio.Event()

    async def hold() -> None:
        async with limiter.slot():
            await release.wait()

    holder = asyncio.create_task(hold())
    waiter = asyncio.create_task(hold())
    await asyncio.sleep(0.01)
    assert limiter.active == 1 and limiter.queued == 1
    with pytest.raises(SandboxBusyError):
        await limiter.acquire()
    release.set()
    await asyncio.gather(holder, waiter)
    assert limiter.active == 0


async def test_cancelled_waiter_frees_queue() -> None:
    limiter = ConcurrencyLimiter(max_concurrency=1, max_queue=1)
    await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert limiter.queued == 0
    limiter.release()
    assert limiter.active == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-05-01T10:20:30.123456789Z", datetime(2024, 5, 1, 10, 20, 30, 123456, tzinfo=timezone.utc)),
        ("2024-05-01T10:20:30Z", datetime(2024, 5, 1, 10, 20, 30, tzinfo=timezone.utc)),
    ],
)
def test_parse_docker_timestamp(raw: str, expected: datetime) -> None:
    assert parse_docker_timestamp(raw) == expected
