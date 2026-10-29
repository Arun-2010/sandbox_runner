"""Test doubles shared by the sandbox, agent and service tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from agentkit.sandbox.base import Sandbox
from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.models import ExecutionRequest, ExecutionResult, ExecutionStatus


def make_result(status: ExecutionStatus = ExecutionStatus.OK, **fields: Any) -> ExecutionResult:
    base: dict[str, Any] = {
        "run_id": "fake",
        "status": status,
        "exit_code": 0 if status is ExecutionStatus.OK else 1,
        "duration_ms": 5,
        "backend": "fake",
    }
    base.update(fields)
    return ExecutionResult(**base)


class FakeSandbox(Sandbox):
    backend = "fake"

    def __init__(
        self,
        results: list[ExecutionResult | Exception] | Callable[[ExecutionRequest], ExecutionResult] | None = None,
        *,
        config: SandboxConfig | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.config = config or SandboxConfig()
        self._results = results if results is not None else []
        self.requests: list[ExecutionRequest] = []
        self.delay_s = delay_s
        self.healthy = True

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if callable(self._results):
            return self._results(request)
        item = self._results.pop(0) if self._results else make_result()
        if isinstance(item, Exception):
            raise item
        return item

    async def health(self) -> dict[str, Any]:
        from agentkit.sandbox.errors import SandboxUnavailableError

        if not self.healthy:
            raise SandboxUnavailableError("fake is down")
        return {"backend": self.backend}
