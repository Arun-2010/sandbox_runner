"""LocalSubprocessSandbox: runs the sandbox runner as a plain host subprocess. UNSAFE.

Why it exists: most of the runner's logic (timeouts, output caps, artifact collection) can be
tested without Docker, and contributors on machines without Docker still need a fast dev loop.
It provides NO isolation: code can read host files and reach the network. It refuses to
construct unless the caller passes `allow_unsafe=True`, and nothing ever falls back to it.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agentkit.sandbox.base import ManagedSandbox, elapsed_ms, log_run
from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.errors import SandboxUnavailableError
from agentkit.sandbox.models import AppliedLimits, ExecutionRequest, ExecutionResult
from agentkit.sandbox.process import run_bounded
from agentkit.sandbox.results import build_result

RUNNER_PATH = Path(__file__).with_name("runner.py")
MAX_RUNNER_STDERR = 64 * 1024
logger = logging.getLogger("agentkit.sandbox")


class LocalSubprocessSandbox(ManagedSandbox):
    backend = "local"

    def __init__(self, config: SandboxConfig | None = None, *, allow_unsafe: bool = False) -> None:
        if not allow_unsafe:
            raise SandboxUnavailableError(
                "LocalSubprocessSandbox runs code on the host with no isolation and is for "
                "development and tests only. Pass allow_unsafe=True to acknowledge this, or use "
                "DockerSandbox."
            )
        super().__init__(config)
        logger.warning("LocalSubprocessSandbox in use: sandboxed code is NOT isolated from this host")

    async def health(self) -> dict[str, Any]:
        return {"backend": self.backend, "isolated": False, "python": sys.version.split()[0]}

    def applied_limits(self, timeout_s: float) -> AppliedLimits:
        return AppliedLimits(
            isolated=False,
            network="unrestricted",
            timeout_s=timeout_s,
            max_stdout_bytes=self.config.max_stdout_bytes,
            max_stderr_bytes=self.config.max_stderr_bytes,
        )

    async def _execute(self, request: ExecutionRequest, run_id: str, timeout_s: float) -> ExecutionResult:
        payload, nonce = self.build_payload(request, timeout_s)
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="agentkit-local-", ignore_cleanup_errors=True) as tmp:
            workspace = os.path.join(tmp, "workspace")
            cmd = [
                sys.executable, "-I", str(RUNNER_PATH),
                "--workspace", workspace,
                "--max-payload-bytes", str(self.config.max_payload_bytes),
            ]  # fmt: skip
            outcome = await run_bounded(
                cmd,
                stdin=payload,
                timeout_s=timeout_s + self.config.startup_grace_s,
                max_stdout=self.config.max_result_bytes,
                max_stderr=MAX_RUNNER_STDERR,
                env=_minimal_env(tmp),
            )
        result = build_result(
            outcome=outcome,
            nonce=nonce,
            run_id=run_id,
            backend=self.backend,
            limits=self.applied_limits(timeout_s),
            config=self.config,
            duration_ms=elapsed_ms(start),
        )
        log_run(result, bytes_in=len(payload), bytes_out=outcome.stdout_total)
        return result


def _minimal_env(tmp: str) -> dict[str, str]:
    """Do not hand host secrets to the child, even in the unsafe backend."""
    env = {
        "PATH": os.defpath,
        "HOME": tmp,
        "TMP": tmp,
        "TEMP": tmp,
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": os.path.join(tmp, "mpl"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]  # the interpreter cannot start without it
    return env
