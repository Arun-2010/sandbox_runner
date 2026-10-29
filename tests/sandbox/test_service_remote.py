from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from agentkit.sandbox.config import SandboxConfig  # noqa: E402
from agentkit.sandbox.errors import (  # noqa: E402
    SandboxBusyError,
    SandboxError,
    SandboxUnavailableError,
)
from agentkit.sandbox.models import ExecutionRequest, ExecutionStatus  # noqa: E402
from agentkit.sandbox.remote import RemoteSandbox  # noqa: E402
from services.executor.app import create_app  # noqa: E402
from tests.sandbox.helpers import FakeSandbox, make_result  # noqa: E402

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"code": "print(1)"}


def client_for(sandbox: FakeSandbox, **kwargs) -> TestClient:
    return TestClient(create_app(sandbox, token=TOKEN, janitor=False, **kwargs))


def test_auth_required() -> None:
    with client_for(FakeSandbox()) as client:
        assert client.post("/v1/executions", json=BODY).status_code == 401
        assert client.post("/v1/executions", json=BODY, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/healthz").status_code == 200


def test_happy_path() -> None:
    sandbox = FakeSandbox([make_result(stdout="1\n")])
    with client_for(sandbox) as client:
        response = client.post("/v1/executions", json=BODY, headers=AUTH)
    assert response.status_code == 200 and response.json()["stdout"] == "1\n"
    assert sandbox.requests[0].code == "print(1)"


def test_body_too_large_413() -> None:
    sandbox = FakeSandbox(config=SandboxConfig(max_input_bytes=0, max_code_chars=10, max_input_files=0))
    with client_for(sandbox) as client:
        response = client.post("/v1/executions", content=b"x" * 200_000, headers=AUTH)
    assert response.status_code == 413 and not sandbox.requests


def test_invalid_body_422() -> None:
    with client_for(FakeSandbox()) as client:
        response = client.post("/v1/executions", json={"code": "", "evil": 1}, headers=AUTH)
    assert response.status_code == 422


def test_queue_full_429() -> None:
    with client_for(FakeSandbox([SandboxBusyError("busy", retry_after_s=2)])) as client:
        response = client.post("/v1/executions", json=BODY, headers=AUTH)
    assert response.status_code == 429 and response.headers["Retry-After"] == "2"


def test_unavailable_503_and_readyz() -> None:
    sandbox = FakeSandbox([SandboxUnavailableError("docker down")])
    with client_for(sandbox) as client:
        assert client.post("/v1/executions", json=BODY, headers=AUTH).status_code == 503
        assert client.get("/readyz").status_code == 200
        sandbox.healthy = False
        assert client.get("/readyz").status_code == 503


def test_refuses_to_start_without_token(monkeypatch) -> None:
    monkeypatch.delenv("AGENTKIT_EXECUTOR_TOKEN", raising=False)
    monkeypatch.delenv("AGENTKIT_EXECUTOR_DEV", raising=False)
    app = create_app(FakeSandbox(), janitor=False)
    with pytest.raises(RuntimeError, match="refusing to start"), TestClient(app):
        pass


# --------------------------------------------------------------------------- RemoteSandbox


class AppTransport(httpx.AsyncBaseTransport):
    """Routes httpx requests into the app via TestClient on a worker thread."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()

        def send() -> httpx.Response:
            return self.client.request(request.method, str(request.url.path), content=body, headers=request.headers)

        resp = await asyncio.to_thread(send)
        return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content)


def test_remote_sandbox_roundtrip() -> None:
    with client_for(FakeSandbox([make_result(stdout="remote!")])) as client:
        remote = RemoteSandbox("http://executor", TOKEN, transport=AppTransport(client))
        result = remote.run_sync(ExecutionRequest(code="print(1)"))
        assert result.stdout == "remote!"
        assert remote.health_sync()["backend"] == "remote"


def test_remote_maps_errors() -> None:
    with client_for(FakeSandbox([SandboxBusyError("busy")])) as client:
        remote = RemoteSandbox("http://executor", TOKEN, transport=AppTransport(client))
        with pytest.raises(SandboxBusyError):
            remote.run_sync(ExecutionRequest(code="1"))
    with client_for(FakeSandbox()) as client:
        bad = RemoteSandbox("http://executor", "wrong", transport=AppTransport(client))
        with pytest.raises(SandboxError, match="credentials"):
            bad.run_sync(ExecutionRequest(code="1"))


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise self.exc


def test_remote_retries_only_connection_errors() -> None:
    connect = CountingTransport(httpx.ConnectError("refused"))
    with pytest.raises(SandboxUnavailableError):
        RemoteSandbox("http://x", TOKEN, transport=connect, connect_retries=2).run_sync(ExecutionRequest(code="1"))
    assert connect.calls == 3

    read = CountingTransport(httpx.ReadTimeout("slow"))
    with pytest.raises(SandboxError, match="may have executed"):
        RemoteSandbox("http://x", TOKEN, transport=read, connect_retries=2).run_sync(ExecutionRequest(code="1"))
    assert read.calls == 1


def test_remote_rejected_maps_to_status() -> None:
    with client_for(FakeSandbox()) as client:
        remote = RemoteSandbox("http://executor", TOKEN, transport=AppTransport(client))
        result = remote.run_sync(ExecutionRequest.model_construct(code="", files=[], timeout_s=None))
    assert result.status is ExecutionStatus.REJECTED
