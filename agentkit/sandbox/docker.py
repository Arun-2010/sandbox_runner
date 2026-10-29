"""DockerSandbox: one fresh, hardened, throwaway container per execution.

Why: isolation must come from the container runtime, not from inspecting code. Each run gets
a new container with no network, a read-only root, a non-root user, no capabilities, cgroup
limits and tmpfs-only writable space, and is destroyed afterwards. Inputs travel on stdin
(`docker run -i`) because bind mounts would expose host paths and `docker cp` cannot write
into a tmpfs. The CLI is used instead of the SDK: it behaves the same on Linux, macOS and
Docker Desktop for Windows and avoids the SDK's stdin socket multiplexing.

Fail closed: if Docker, the image, the configured runtime or a cgroup limit is unavailable,
`health()` raises `SandboxUnavailableError` and no code runs.

OOM detection: `--rm` removes the container, so `State.OOMKilled` cannot be inspected after
the fact. Instead, exit 137 with no runner result (the runner itself was killed), or a child
killed by SIGKILL / a `MemoryError` reported by the runner, maps to status `oom`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from agentkit.sandbox.base import ManagedSandbox, elapsed_ms, log_run
from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.errors import SandboxUnavailableError
from agentkit.sandbox.models import AppliedLimits, ExecutionRequest, ExecutionResult, ExecutionStatus
from agentkit.sandbox.process import run_bounded, run_cli
from agentkit.sandbox.results import build_result, failure_result

logger = logging.getLogger("agentkit.sandbox")

SANDBOX_UID = 10001
CONTAINER_PREFIX = "agentkit-sbx-"
SANDBOX_LABEL = "agentkit.sandbox=1"
RUN_ID_LABEL = "agentkit.run_id"
# The only environment variables the container gets. Values are always explicit (`KEY=VALUE`):
# a bare `-e KEY` would copy the host's value into the sandbox.
CONTAINER_ENV = {
    "HOME": "/tmp",
    "MPLCONFIGDIR": "/tmp/mpl",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}
DOCKER_CLI_ERRORS = frozenset({125, 126, 127})  # daemon/CLI failure, not the sandboxed code
REQUIRED_CGROUP_FEATURES = ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota")
MAX_DOCKER_STDERR = 64 * 1024
CLI_TIMEOUT_S = 15.0
_RUN_ID_RE = re.compile(r"[a-f0-9]{8,64}")


def container_name(run_id: str) -> str:
    return CONTAINER_PREFIX + run_id


def build_docker_command(config: SandboxConfig, run_id: str) -> list[str]:
    """The complete `docker run` argument list. Every flag here is required; see SECURITY.md."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must be lowercase hex")
    mem = f"{config.memory_mb}m"
    pids = config.pids_limit
    ids = f"uid={SANDBOX_UID},gid={SANDBOX_UID},mode=0700"
    cmd = [
        config.docker_bin, "run", "--rm", "-i",
        "--name", container_name(run_id),
        "--label", SANDBOX_LABEL,
        "--label", f"{RUN_ID_LABEL}={run_id}",
        "--pull", "never",
        "--init",
        "--network", "none",
        "--read-only",
        "--tmpfs", f"/workspace:rw,nosuid,nodev,size={config.workspace_mb}m,{ids}",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={config.tmp_mb}m,{ids}",
        "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", mem,
        "--memory-swap", mem,
        "--cpus", f"{config.cpus:g}",
        "--pids-limit", str(pids),
        "--ulimit", f"nofile={config.nofile}:{config.nofile}",
        "--ulimit", f"nproc={pids}:{pids}",
        "--hostname", "sandbox",
        "--workdir", "/workspace",
        "--log-driver", "none",
    ]  # fmt: skip
    for key, value in CONTAINER_ENV.items():
        cmd += ["--env", f"{key}={value}"]
    if config.runtime:
        cmd += ["--runtime", config.runtime]
    cmd += [config.image, "--container", "--max-payload-bytes", str(config.max_payload_bytes)]
    return cmd


class DockerSandbox(ManagedSandbox):
    backend = "docker"

    def __init__(self, config: SandboxConfig | None = None, *, health_ttl_s: float = 30.0) -> None:
        super().__init__(config)
        self.health_ttl_s = health_ttl_s
        self._healthy_until = 0.0

    # ------------------------------------------------------------------ health

    async def health(self) -> dict[str, Any]:
        docker = self.config.docker_bin
        build_hint = f"run: docker build -t {self.config.image} sandbox_image/"
        try:
            rc, version, _ = await run_cli([docker, "version", "--format", "{{.Server.Version}}"], CLI_TIMEOUT_S)
        except FileNotFoundError as exc:
            raise SandboxUnavailableError(f"docker CLI {docker!r} not found; install Docker") from exc
        if rc != 0:
            raise SandboxUnavailableError("Docker daemon is not reachable (`docker version` failed); start Docker")

        rc, info_json, _ = await run_cli([docker, "info", "--format", "{{json .}}"], CLI_TIMEOUT_S)
        if rc != 0:
            raise SandboxUnavailableError("`docker info` failed; cannot verify that limits can be enforced")
        try:
            info = json.loads(info_json)
        except json.JSONDecodeError as exc:
            raise SandboxUnavailableError("`docker info` returned unexpected output") from exc
        missing = [f for f in REQUIRED_CGROUP_FEATURES if info.get(f) is False]
        if missing:
            raise SandboxUnavailableError(
                f"Docker cannot enforce required limits ({', '.join(missing)}); refusing to run"
            )
        if self.config.runtime and self.config.runtime not in (info.get("Runtimes") or {}):
            raise SandboxUnavailableError(f"container runtime {self.config.runtime!r} is not installed")

        rc, image_id, _ = await run_cli(
            [docker, "image", "inspect", "--format", "{{.Id}}", self.config.image], CLI_TIMEOUT_S
        )
        if rc != 0:
            raise SandboxUnavailableError(f"sandbox image {self.config.image!r} not found; {build_hint}")
        return {
            "backend": self.backend,
            "isolated": True,
            "docker_version": version.strip(),
            "image": self.config.image,
            "image_id": image_id.strip(),
            "runtime": self.config.runtime or info.get("DefaultRuntime"),
        }

    async def _ensure_healthy(self) -> None:
        if time.monotonic() < self._healthy_until:
            return
        await self.health()
        self._healthy_until = time.monotonic() + self.health_ttl_s

    # ------------------------------------------------------------------ execution

    def applied_limits(self, timeout_s: float) -> AppliedLimits:
        cfg = self.config
        return AppliedLimits(
            isolated=True,
            network="none",
            timeout_s=timeout_s,
            memory_mb=cfg.memory_mb,
            cpus=cfg.cpus,
            pids_limit=cfg.pids_limit,
            workspace_mb=cfg.workspace_mb,
            tmp_mb=cfg.tmp_mb,
            runtime=cfg.runtime,
            max_stdout_bytes=cfg.max_stdout_bytes,
            max_stderr_bytes=cfg.max_stderr_bytes,
        )

    async def _execute(self, request: ExecutionRequest, run_id: str, timeout_s: float) -> ExecutionResult:
        await self._ensure_healthy()
        payload, nonce = self.build_payload(request, timeout_s)
        name = container_name(run_id)
        limits = self.applied_limits(timeout_s)
        start = time.monotonic()

        aborted = False

        async def abort() -> None:
            nonlocal aborted
            aborted = True
            await self._kill_container(name)

        try:
            outcome = await run_bounded(
                build_docker_command(self.config, run_id),
                stdin=payload,
                timeout_s=timeout_s + self.config.startup_grace_s,
                max_stdout=self.config.max_result_bytes,
                max_stderr=MAX_DOCKER_STDERR,
                on_abort=abort,
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailableError("docker CLI not found", run_id=run_id) from exc
        if not outcome.clean_exit and not aborted:
            await self._remove_container(name)  # abort() already killed (and if needed removed) it

        if outcome.clean_exit and outcome.returncode in DOCKER_CLI_ERRORS and not outcome.stdout:
            self._healthy_until = 0.0  # re-check health before the next run
            logger.error(
                "docker run failed run_id=%s rc=%s stderr=%r",
                run_id,
                outcome.returncode,
                outcome.stderr[:500].decode("utf-8", "replace"),
            )
            result = failure_result(
                run_id=run_id,
                status=ExecutionStatus.SANDBOX_ERROR,
                message=f"docker could not start the sandbox container (exit {outcome.returncode})",
                backend=self.backend,
                limits=limits,
                duration_ms=elapsed_ms(start),
            )
        else:
            result = build_result(
                outcome=outcome,
                nonce=nonce,
                run_id=run_id,
                backend=self.backend,
                limits=limits,
                config=self.config,
                duration_ms=elapsed_ms(start),
            )
        log_run(result, bytes_in=len(payload), bytes_out=outcome.stdout_total)
        return result

    async def _kill_container(self, name: str) -> None:
        rc, _, _ = await run_cli([self.config.docker_bin, "kill", name], CLI_TIMEOUT_S)
        if rc != 0:
            await self._remove_container(name)

    async def _remove_container(self, name: str) -> None:
        rc, _, stderr = await run_cli([self.config.docker_bin, "rm", "-f", name], CLI_TIMEOUT_S)
        if rc != 0 and "No such container" not in stderr:
            logger.error("failed to remove sandbox container %s; the janitor will retry", name)
