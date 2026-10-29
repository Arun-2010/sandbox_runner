"""RemoteSandbox: the Sandbox interface over HTTP to the executor service.

Why: whoever can talk to the Docker daemon is effectively root on that host, so in production
the agent process should not hold the Docker socket. It calls a dedicated executor service
instead. Retries happen only when the connection was never established: a request that may
have reached the server is never re-sent, because it may already have executed.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from pydantic import ValidationError

from agentkit.sandbox.base import Sandbox
from agentkit.sandbox.errors import SandboxBusyError, SandboxError, SandboxUnavailableError
from agentkit.sandbox.models import ExecutionRequest, ExecutionResult, ExecutionStatus
from agentkit.sandbox.results import failure_result

try:
    import httpx
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("RemoteSandbox needs httpx: pip install 'agentkit[service]'") from exc

DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_SERVER_TIMEOUT_S = 60.0


class RemoteSandbox(Sandbox):
    backend = "remote"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connect_timeout_s: float = 5.0,
        extra_timeout_s: float = 30.0,
        connect_retries: int = 2,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.connect_timeout_s = connect_timeout_s
        self.extra_timeout_s = extra_timeout_s
        self.connect_retries = connect_retries
        self.max_response_bytes = max_response_bytes
        self._transport = transport

    def _client(self, read_timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=httpx.Timeout(read_timeout_s, connect=self.connect_timeout_s),
            transport=self._transport,
        )

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        read_timeout = (request.timeout_s or DEFAULT_SERVER_TIMEOUT_S) + self.extra_timeout_s
        body = request.model_dump_json().encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.connect_retries + 1):
            try:
                async with (
                    self._client(read_timeout) as client,
                    client.stream(
                        "POST", "/v1/executions", content=body, headers={"Content-Type": "application/json"}
                    ) as response,
                ):
                    data = await self._read_capped(response)
                    return self._handle(response, data)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                if attempt < self.connect_retries:
                    await asyncio.sleep(0.2 * 2**attempt)
            except httpx.TimeoutException as exc:
                raise SandboxError("executor did not answer in time; the run may have executed") from exc
            except httpx.HTTPError as exc:
                raise SandboxError(f"executor request failed: {type(exc).__name__}") from exc
        raise SandboxUnavailableError(f"cannot connect to executor at {self.base_url}") from last_error

    async def _read_capped(self, response: httpx.Response) -> bytes:
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.max_response_bytes:
                raise SandboxError("executor response exceeded the size limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _handle(self, response: httpx.Response, data: bytes) -> ExecutionResult:
        status = response.status_code
        if status == 200:
            try:
                return ExecutionResult.model_validate_json(data)
            except ValidationError as exc:
                raise SandboxError("executor returned a malformed result") from exc
        if status in (413, 422):
            return failure_result(
                run_id=uuid.uuid4().hex,
                status=ExecutionStatus.REJECTED,
                message=f"executor rejected the request (HTTP {status})",
                backend=self.backend,
                limits=None,
                duration_ms=0,
            )
        if status == 429:
            retry_after = _float_or(response.headers.get("Retry-After"), 1.0)
            raise SandboxBusyError("executor is busy", retry_after_s=retry_after)
        if status == 503:
            raise SandboxUnavailableError("executor reports the sandbox is unavailable")
        if status in (401, 403):
            raise SandboxError("executor rejected the credentials")
        raise SandboxError(f"executor returned HTTP {status}")

    async def health(self) -> dict[str, Any]:
        try:
            async with self._client(10.0) as client:
                response = await client.get("/readyz")
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError(f"cannot reach executor at {self.base_url}") from exc
        if response.status_code != 200:
            raise SandboxUnavailableError(f"executor not ready (HTTP {response.status_code})")
        return {"backend": self.backend, "url": self.base_url, "executor": response.json()}


def _float_or(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default
