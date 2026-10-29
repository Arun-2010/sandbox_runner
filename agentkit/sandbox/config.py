"""Sandbox limits and deployment settings.

Why: every isolation limit is a number someone will want to tune per deployment, and every
one of them must be impossible to switch off by accident. The model gives each limit a hard
lower bound (no "0 = unlimited") and checks that the limits are mutually consistent, so a bad
environment variable fails at startup instead of silently weakening isolation.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

KB = 1024
MB = 1024 * 1024
ENV_PREFIX = "AGENTKIT_SANDBOX_"
DEFAULT_IMAGE = "agentkit-sandbox:dev"

# Image refs and runtime names end up as `docker run` arguments; a leading "-" would be parsed
# as a flag, so both are restricted to a conservative character set.
_IMAGE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$"
_RUNTIME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str = Field(DEFAULT_IMAGE, pattern=_IMAGE_PATTERN)
    runtime: str | None = Field(None, pattern=_RUNTIME_PATTERN)
    docker_bin: str = Field("docker", min_length=1)

    timeout_s: float = Field(10.0, gt=0, le=3600)
    max_timeout_s: float = Field(60.0, gt=0, le=3600)
    startup_grace_s: float = Field(5.0, ge=1, le=120)

    memory_mb: int = Field(512, ge=64, le=65536)
    cpus: float = Field(1.0, gt=0, le=64)
    pids_limit: int = Field(64, ge=16, le=4096)
    nofile: int = Field(256, ge=32, le=65536)
    workspace_mb: int = Field(64, ge=8, le=4096)
    tmp_mb: int = Field(32, ge=8, le=4096)

    max_code_chars: int = Field(100_000, ge=1, le=1_000_000)
    max_input_files: int = Field(10, ge=0, le=1000)
    max_input_bytes: int = Field(20 * MB, ge=0, le=1024 * MB)
    max_stdout_bytes: int = Field(64 * KB, ge=1 * KB, le=64 * MB)
    max_stderr_bytes: int = Field(64 * KB, ge=1 * KB, le=64 * MB)
    max_artifacts: int = Field(10, ge=0, le=1000)
    max_artifact_bytes: int = Field(5 * MB, ge=0, le=1024 * MB)
    max_total_artifact_bytes: int = Field(10 * MB, ge=0, le=1024 * MB)
    max_result_bytes: int = Field(20 * MB, ge=64 * KB, le=2048 * MB)

    max_concurrency: int = Field(4, ge=1, le=256)
    max_queue: int = Field(16, ge=0, le=10_000)

    @model_validator(mode="after")
    def _check_consistency(self) -> SandboxConfig:
        if self.timeout_s > self.max_timeout_s:
            raise ValueError("timeout_s must not exceed max_timeout_s")
        if self.max_input_bytes > self.workspace_mb * MB:
            raise ValueError("max_input_bytes must fit in the /workspace tmpfs (workspace_mb)")
        if self.max_artifact_bytes > self.max_total_artifact_bytes:
            raise ValueError("max_artifact_bytes must not exceed max_total_artifact_bytes")
        # The runner's JSON line must fit in what the host is willing to read: base64 artifacts
        # plus worst-case JSON escaping of the captured streams plus envelope overhead.
        needed = (
            math.ceil(self.max_total_artifact_bytes * 4 / 3)
            + 6 * (self.max_stdout_bytes + self.max_stderr_bytes)
            + 64 * KB
        )
        if self.max_result_bytes < needed:
            raise ValueError(f"max_result_bytes must be at least {needed} for these limits")
        return self

    @property
    def max_payload_bytes(self) -> int:
        """Upper bound on the JSON payload sent to the runner (base64 files, escaped code)."""
        return (
            math.ceil(self.max_input_bytes * 4 / 3)
            + 6 * self.max_code_chars
            + 256 * max(self.max_input_files, 1)
            + 64 * KB
        )

    @property
    def outer_timeout_s(self) -> float:
        return self.max_timeout_s + self.startup_grace_s

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> SandboxConfig:
        """Build from AGENTKIT_SANDBOX_<FIELD> variables (e.g. AGENTKIT_SANDBOX_MEMORY_MB=1024)."""
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            key = ENV_PREFIX + name.upper()
            if key in env:
                raw = env[key].strip()
                values[name] = None if (name == "runtime" and raw == "") else raw
        values.update(overrides)
        return cls.model_validate(values)
