"""Run a subprocess with a wall-clock timeout and hard caps on what we read from it.

Why: the process on the other end of the pipe is controlled by untrusted code. Reading its
output with `communicate()` would let it make the host buffer gigabytes. This helper reads in
chunks, stops at a byte cap, enforces an outer deadline, and always runs an abort hook (for
Docker: `docker kill`) when the run did not end cleanly, including on cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

READ_CHUNK = 64 * 1024
KILL_WAIT_S = 10.0


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_overflow: bool
    stdout_total: int

    @property
    def clean_exit(self) -> bool:
        return not self.timed_out and not self.stdout_overflow and self.returncode is not None


async def run_bounded(
    cmd: Sequence[str],
    *,
    stdin: bytes = b"",
    timeout_s: float,
    max_stdout: int,
    max_stderr: int,
    env: Mapping[str, str] | None = None,
    on_abort: Callable[[], Awaitable[None]] | None = None,
) -> ProcessOutcome:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    out_buf, err_buf = bytearray(), bytearray()
    out_total = 0
    overflow = asyncio.Event()

    async def feed() -> None:
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the process exited early; its exit status tells the story
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()

    async def read_stdout() -> None:
        nonlocal out_total
        while chunk := await proc.stdout.read(READ_CHUNK):
            out_total += len(chunk)
            room = max_stdout - len(out_buf)
            out_buf.extend(chunk[: max(room, 0)])
            if len(chunk) > room:
                overflow.set()
                return  # stop reading; the caller kills the process

    async def read_stderr() -> None:
        while chunk := await proc.stderr.read(READ_CHUNK):
            room = max_stderr - len(err_buf)
            if room > 0:
                err_buf.extend(chunk[:room])  # keep draining past the cap so the writer never blocks

    feeder = asyncio.create_task(feed())
    work = asyncio.ensure_future(asyncio.gather(read_stdout(), read_stderr(), proc.wait()))
    overflow_wait = asyncio.create_task(overflow.wait())
    timed_out = False
    clean = False
    try:
        done, _ = await asyncio.wait({work, overflow_wait}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        if work in done and not overflow.is_set():
            work.result()
            clean = True
        else:
            timed_out = not overflow.is_set()
    finally:
        overflow_wait.cancel()
        if not clean:
            await asyncio.shield(_abort(proc, on_abort))
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder

    return ProcessOutcome(
        returncode=proc.returncode,
        stdout=bytes(out_buf),
        stderr=bytes(err_buf),
        timed_out=timed_out,
        stdout_overflow=overflow.is_set(),
        stdout_total=out_total,
    )


async def _abort(proc: asyncio.subprocess.Process, on_abort: Callable[[], Awaitable[None]] | None) -> None:
    if on_abort is not None:
        await on_abort()
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), KILL_WAIT_S)


async def run_cli(cmd: Sequence[str], timeout_s: float = 30.0) -> tuple[int | None, str, str]:
    """Run a short trusted command (e.g. `docker version`). Returns (returncode, stdout, stderr);
    returncode is None if it timed out. Raises FileNotFoundError if the binary is missing."""
    outcome = await run_bounded(cmd, timeout_s=timeout_s, max_stdout=4 * 1024 * 1024, max_stderr=64 * 1024)
    rc = None if outcome.timed_out else outcome.returncode
    return rc, outcome.stdout.decode("utf-8", "replace"), outcome.stderr.decode("utf-8", "replace")
