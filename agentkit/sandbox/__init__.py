"""Sandboxed execution of untrusted, LLM-written Python (Project 4).

Why: an agent that writes code must run it somewhere that cannot read host files, reach the
network, exhaust the host, persist between runs or forge its own results. This package hides
that behind one `Sandbox` interface with a hardened Docker backend (default), an HTTP client
for a remote executor, and an explicitly unsafe local backend for tests. `RemoteSandbox` lives
in `agentkit.sandbox.remote` because it needs the optional `httpx` dependency.
"""

from __future__ import annotations

from agentkit.sandbox.base import ManagedSandbox, Sandbox
from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.docker import DockerSandbox, build_docker_command
from agentkit.sandbox.errors import (
    RequestRejectedError,
    SandboxBusyError,
    SandboxError,
    SandboxUnavailableError,
)
from agentkit.sandbox.formatting import to_llm_text
from agentkit.sandbox.limiter import ConcurrencyLimiter
from agentkit.sandbox.local import LocalSubprocessSandbox
from agentkit.sandbox.models import (
    AppliedLimits,
    Artifact,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    InputFile,
)
from agentkit.sandbox.tool import RunPythonArgs, register_final_answer, register_run_python

__all__ = [
    "AppliedLimits",
    "Artifact",
    "ConcurrencyLimiter",
    "DockerSandbox",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionStatus",
    "InputFile",
    "LocalSubprocessSandbox",
    "ManagedSandbox",
    "RequestRejectedError",
    "RunPythonArgs",
    "Sandbox",
    "SandboxBusyError",
    "SandboxConfig",
    "SandboxError",
    "SandboxUnavailableError",
    "build_docker_command",
    "register_final_answer",
    "register_run_python",
    "to_llm_text",
]
