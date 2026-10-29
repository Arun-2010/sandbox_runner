"""FastAPI executor service: the only process that talks to the Docker daemon.

Why: access to the Docker socket is root on the host. Agents run elsewhere and call this
service over HTTP with a bearer token; the service applies the same limits, backpressure and
fail-closed behaviour as DockerSandbox and maps them to HTTP (413, 429 + Retry-After, 503).

Run: AGENTKIT_EXECUTOR_TOKEN=... uvicorn services.executor.app:app --port 8080
The service refuses to start without a token unless AGENTKIT_EXECUTOR_DEV=1.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from agentkit.router import summarise_validation
from agentkit.sandbox.base import Sandbox
from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.docker import DockerSandbox
from agentkit.sandbox.errors import SandboxBusyError, SandboxError, SandboxUnavailableError
from agentkit.sandbox.janitor import remove_stale_containers, run_janitor_forever
from agentkit.sandbox.models import ExecutionRequest, ExecutionResult

logger = logging.getLogger("agentkit.executor")
TOKEN_ENV = "AGENTKIT_EXECUTOR_TOKEN"
DEV_ENV = "AGENTKIT_EXECUTOR_DEV"
DEV_TOKEN = "dev-token"


def _max_body_bytes(config: SandboxConfig) -> int:
    return config.max_payload_bytes


def create_app(
    sandbox: Sandbox | None = None,
    *,
    config: SandboxConfig | None = None,
    token: str | None = None,
    dev: bool | None = None,
    janitor: bool | None = None,
) -> FastAPI:
    """Build the app. Arguments default to environment configuration (used by uvicorn);
    tests inject a fake sandbox and a token."""
    state: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        dev_mode = dev if dev is not None else os.environ.get(DEV_ENV) == "1"
        secret = token if token is not None else os.environ.get(TOKEN_ENV, "")
        if not secret:
            if not dev_mode:
                raise RuntimeError(f"{TOKEN_ENV} is not set; refusing to start (set {DEV_ENV}=1 for local dev)")
            secret = DEV_TOKEN
            logger.warning("executor running in DEV mode with token %r", DEV_TOKEN)
        cfg = config or getattr(sandbox, "config", None) or SandboxConfig.from_env()
        box = sandbox or DockerSandbox(cfg)
        state.update(token=secret.encode(), config=cfg, sandbox=box)

        task = None
        if janitor if janitor is not None else isinstance(box, DockerSandbox):
            try:
                await remove_stale_containers(cfg)
            except (SandboxError, OSError) as exc:
                logger.warning("startup janitor pass failed: %s", exc)
            task = asyncio.create_task(run_janitor_forever(cfg))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="agentkit executor", version="0.4.0", lifespan=lifespan)

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), state["token"]):
            raise HTTPException(401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Unauthenticated so orchestrator probes work; it reveals only ready / not ready.
    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            await state["sandbox"].health()
        except SandboxUnavailableError as exc:
            logger.warning("not ready: %s", exc)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/executions", response_model=ExecutionResult, dependencies=[Depends(require_token)])
    async def create_execution(request: Request) -> ExecutionResult:
        limit = _max_body_bytes(state["config"])
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise HTTPException(413, f"request body exceeds {limit} bytes")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise HTTPException(413, f"request body exceeds {limit} bytes")
        try:
            execution = ExecutionRequest.model_validate_json(bytes(body))
        except ValidationError as exc:
            raise HTTPException(422, summarise_validation(exc)) from exc
        try:
            return await state["sandbox"].run(execution)
        except SandboxBusyError as exc:
            raise HTTPException(
                429, "sandbox is busy", headers={"Retry-After": str(max(1, round(exc.retry_after_s)))}
            ) from exc
        except SandboxUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
        except SandboxError as exc:
            logger.error("sandbox failure run_id=%s: %s", exc.run_id, exc)
            raise HTTPException(500, "sandbox failure") from exc

    return app


app = create_app()
