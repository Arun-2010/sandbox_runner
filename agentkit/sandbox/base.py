"""The Sandbox interface and the shared request pipeline for local backends.

Why: agents and the executor service depend on one small interface (`run`, `run_sync`,
`health`) so the backend can change from Docker to a remote executor without touching them.
`ManagedSandbox` holds the parts every host-side backend must do identically: validate the
request against configured limits, take a concurrency slot, build the runner payload with a
fresh nonce, wrap unexpected failures in `SandboxError`, and log one structured line per run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from agentkit.sandbox.config import MB, SandboxConfig
from agentkit.sandbox.errors import RequestRejectedError, SandboxError
from agentkit.sandbox.limiter import ConcurrencyLimiter
from agentkit.sandbox.models import (
    AppliedLimits,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    RunnerLimits,
    RunnerPayload,
)
from agentkit.sandbox.results import failure_result

logger = logging.getLogger("agentkit.sandbox")


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("run_sync() called from a running event loop; use `await sandbox.run(...)`")


class Sandbox(ABC):
    backend: ClassVar[str]

    @abstractmethod
    async def run(self, request: ExecutionRequest) -> ExecutionResult: ...

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        """Return backend details, or raise SandboxUnavailableError."""

    def run_sync(self, request: ExecutionRequest) -> ExecutionResult:
        return _run_sync(self.run(request))

    def health_sync(self) -> dict[str, Any]:
        return _run_sync(self.health())


def check_request(request: ExecutionRequest, config: SandboxConfig) -> float:
    """Apply configured limits. Returns the effective timeout or raises RequestRejectedError."""
    if len(request.code) > config.max_code_chars:
        raise RequestRejectedError(f"code is {len(request.code)} chars; limit is {config.max_code_chars}")
    if len(request.files) > config.max_input_files:
        raise RequestRejectedError(f"{len(request.files)} input files; limit is {config.max_input_files}")
    total = sum(f.size for f in request.files)
    if total > config.max_input_bytes:
        raise RequestRejectedError(f"input files total {total} bytes; limit is {config.max_input_bytes}")
    timeout = request.timeout_s if request.timeout_s is not None else config.timeout_s
    if timeout > config.max_timeout_s:
        raise RequestRejectedError(f"timeout {timeout:g}s exceeds the maximum of {config.max_timeout_s:g}s")
    return timeout


def log_run(result: ExecutionResult, *, bytes_in: int, bytes_out: int) -> None:
    """One structured line per run. Never includes code, output or file contents."""
    logger.info(
        json.dumps(
            {
                "event": "sandbox_run",
                "run_id": result.run_id,
                "backend": result.backend,
                "status": result.status.value,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "bytes_in": bytes_in,
                "bytes_out": bytes_out,
                "artifacts": len(result.artifacts),
            }
        )
    )


class ManagedSandbox(Sandbox):
    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()
        self.limiter = ConcurrencyLimiter(self.config.max_concurrency, self.config.max_queue)

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        run_id = uuid.uuid4().hex
        try:
            timeout_s = check_request(request, self.config)
        except RequestRejectedError as exc:
            result = failure_result(
                run_id=run_id,
                status=ExecutionStatus.REJECTED,
                message=str(exc),
                backend=self.backend,
                limits=None,
                duration_ms=0,
            )
            log_run(result, bytes_in=0, bytes_out=0)
            return result

        async with self.limiter.slot():
            try:
                return await self._execute(request, run_id, timeout_s)
            except SandboxError:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                raise SandboxError(
                    f"{self.backend} sandbox failed: {type(exc).__name__}",
                    run_id=run_id,
                    context={"backend": self.backend},
                ) from exc

    @abstractmethod
    async def _execute(self, request: ExecutionRequest, run_id: str, timeout_s: float) -> ExecutionResult:
        """Run one validated request. Called with a concurrency slot held."""

    @abstractmethod
    def applied_limits(self, timeout_s: float) -> AppliedLimits: ...

    def build_payload(self, request: ExecutionRequest, timeout_s: float) -> tuple[bytes, str]:
        nonce = secrets.token_hex(16)
        cfg = self.config
        payload = RunnerPayload(
            nonce=nonce,
            code=request.code,
            files=request.files,
            timeout_s=timeout_s,
            limits=RunnerLimits(
                max_stdout_bytes=cfg.max_stdout_bytes,
                max_stderr_bytes=cfg.max_stderr_bytes,
                max_artifacts=cfg.max_artifacts,
                max_artifact_bytes=cfg.max_artifact_bytes,
                max_total_artifact_bytes=cfg.max_total_artifact_bytes,
                max_file_bytes=cfg.workspace_mb * MB,
                max_open_files=cfg.nofile,
                cpu_seconds=math.ceil(timeout_s) + 1,
                # cgroup memory limit + 1 GB headroom for mapped-but-untouched address space
                max_address_space_bytes=(cfg.memory_mb + 1024) * MB,
            ),
        )
        return payload.model_dump_json().encode("utf-8"), nonce


def elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))
