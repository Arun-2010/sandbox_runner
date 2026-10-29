"""Data models for sandbox requests, results and the host <-> runner wire format.

Why: everything that crosses a trust boundary is a strict Pydantic model. Requests come from
an LLM (untrusted), and the runner's result comes from inside the sandbox (untrusted: the
sandboxed code runs as the same UID as the runner and may try to forge it). Models reject
unknown fields, and the runner result is validated in strict mode before the host trusts
any of it.
"""

from __future__ import annotations

import base64
import binascii
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
# main.py holds the submitted code and output/ collects artifacts; inputs may not shadow them.
RESERVED_NAMES = frozenset({".", "..", "main.py", "output"})


def is_safe_name(name: object) -> bool:
    """A single path component made of safe characters: no separators, no traversal."""
    return isinstance(name, str) and _NAME_RE.fullmatch(name) is not None and name not in {".", ".."}


def _decode_b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("content_b64 is not valid base64") from exc


def _b64_decoded_len(value: str) -> int:
    return (len(value) * 3) // 4 - value[-2:].count("=")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputFile(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    content_b64: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if value in RESERVED_NAMES:
            raise ValueError(f"file name {value!r} is reserved")
        return value

    @field_validator("content_b64")
    @classmethod
    def _check_b64(cls, value: str) -> str:
        _decode_b64(value)
        return value

    @classmethod
    def from_bytes(cls, name: str, data: bytes) -> InputFile:
        return cls(name=name, content_b64=base64.b64encode(data).decode("ascii"))

    @property
    def size(self) -> int:
        return _b64_decoded_len(self.content_b64)

    def data(self) -> bytes:
        return _decode_b64(self.content_b64)


class ExecutionRequest(_Model):
    code: str = Field(min_length=1, max_length=100_000)
    files: list[InputFile] = Field(default_factory=list, max_length=1000)
    timeout_s: float | None = Field(default=None, gt=0)

    @field_validator("files")
    @classmethod
    def _unique_names(cls, files: list[InputFile]) -> list[InputFile]:
        names = [f.name for f in files]
        if len(names) != len(set(names)):
            raise ValueError("file names must be unique")
        return files


class ExecutionStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    OOM = "oom"
    OUTPUT_LIMIT = "output_limit"
    REJECTED = "rejected"
    SANDBOX_ERROR = "sandbox_error"


class Artifact(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    size: int = Field(ge=0)
    mime: str
    content_b64: str

    def data(self) -> bytes:
        return _decode_b64(self.content_b64)


class AppliedLimits(_Model):
    """The limits actually enforced for a run, so results are auditable."""

    isolated: bool
    network: Literal["none", "unrestricted"]
    timeout_s: float
    memory_mb: int | None = None
    cpus: float | None = None
    pids_limit: int | None = None
    workspace_mb: int | None = None
    tmp_mb: int | None = None
    runtime: str | None = None
    max_stdout_bytes: int
    max_stderr_bytes: int


class ExecutionResult(_Model):
    run_id: str
    status: ExecutionStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    artifacts: list[Artifact] = Field(default_factory=list)
    duration_ms: int = Field(ge=0)
    backend: str
    limits: AppliedLimits | None = None
    # Host-generated explanation (timeouts, skipped artifacts, infra failures). Never contains
    # raw sandbox output, so it is safe to log and to show to the model.
    message: str | None = None


# ---------------------------------------------------------------------------------------------
# Wire format between host and runner. The payload is produced by the host (trusted); the
# result is produced inside the sandbox (untrusted) and is validated in strict mode.


class RunnerLimits(_Model):
    max_stdout_bytes: int = Field(gt=0)
    max_stderr_bytes: int = Field(gt=0)
    max_artifacts: int = Field(ge=0)
    max_artifact_bytes: int = Field(ge=0)
    max_total_artifact_bytes: int = Field(ge=0)
    max_file_bytes: int = Field(gt=0)
    max_open_files: int = Field(gt=0)
    cpu_seconds: int = Field(gt=0)
    max_address_space_bytes: int = Field(gt=0)


class RunnerPayload(_Model):
    version: Literal[1] = 1
    nonce: str = Field(min_length=16, max_length=128)
    code: str
    files: list[InputFile]
    timeout_s: float = Field(gt=0)
    limits: RunnerLimits


class _Untrusted(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunnerArtifact(_Untrusted):
    name: str = Field(max_length=255)
    size: int = Field(ge=0)
    content_b64: str


class RunnerResult(_Untrusted):
    exit_code: int | None
    signal: int | None
    timed_out: bool
    oom_hint: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    artifacts: list[RunnerArtifact] = Field(max_length=1000)
    skipped_artifacts: list[str] = Field(max_length=1000)
    duration_ms: int = Field(ge=0)


class RunnerEnvelope(_Untrusted):
    nonce: str = Field(max_length=128)
    result: RunnerResult
