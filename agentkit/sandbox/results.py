"""Host-side validation of what comes back from the sandbox.

Why: the runner shares a UID with the untrusted program, so every byte it returns is treated
as attacker-controlled. This module finds the runner's result line by a per-run nonce,
validates it strictly, re-applies every size cap, sanitises artifact names, assigns MIME
types from an allow-list, and turns all failure modes into a well-formed `ExecutionResult`
that never echoes raw output.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os

from pydantic import ValidationError

from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.models import (
    AppliedLimits,
    Artifact,
    ExecutionResult,
    ExecutionStatus,
    RunnerArtifact,
    RunnerEnvelope,
    is_safe_name,
)
from agentkit.sandbox.process import ProcessOutcome

# Active formats (HTML, SVG) are deliberately absent: they would be octet-stream, so nothing
# downstream renders attacker-written markup by accident.
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".parquet": "application/vnd.apache.parquet",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
DEFAULT_MIME = "application/octet-stream"
OOM_EXIT_CODE = 137
MAX_MESSAGE_ITEMS = 10


def guess_mime(name: str) -> str:
    return MIME_TYPES.get(os.path.splitext(name)[1].lower(), DEFAULT_MIME)


def cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep head and tail so the total stays within `max_bytes` UTF-8 bytes."""
    data = text.encode("utf-8", "replace")
    if len(data) <= max_bytes:
        return text, False
    marker = "\n...[truncated by host]...\n"
    half = max(0, (max_bytes - len(marker)) // 2)
    head = data[:half].decode("utf-8", "ignore")
    tail = data[len(data) - half :].decode("utf-8", "ignore") if half else ""
    return head + marker + tail, True


def extract_envelope(stdout: bytes, nonce: str) -> RunnerEnvelope | None:
    """Return the last line that is a valid envelope carrying this run's nonce."""
    for line in reversed(stdout.split(b"\n")):
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            envelope = RunnerEnvelope.model_validate_json(line)
        except ValidationError:
            continue
        if hmac.compare_digest(envelope.nonce.encode(), nonce.encode()):
            return envelope
    return None


def sanitize_artifacts(raw: list[RunnerArtifact], config: SandboxConfig) -> tuple[list[Artifact], list[str]]:
    kept: list[Artifact] = []
    dropped: list[str] = []
    seen: set[str] = set()
    total = 0
    for item in raw:
        label = item.name if is_safe_name(item.name) else "<invalid name>"
        if not is_safe_name(item.name) or item.name in seen or len(kept) >= config.max_artifacts:
            dropped.append(label)
            continue
        try:
            data = base64.b64decode(item.content_b64, validate=True)
        except (binascii.Error, ValueError):
            dropped.append(label)
            continue
        size = len(data)
        if size != item.size or size > config.max_artifact_bytes or total + size > config.max_total_artifact_bytes:
            dropped.append(label)
            continue
        seen.add(item.name)
        total += size
        kept.append(Artifact(name=item.name, size=size, mime=guess_mime(item.name), content_b64=item.content_b64))
    return kept, dropped


def failure_result(
    *,
    run_id: str,
    status: ExecutionStatus,
    message: str,
    backend: str,
    limits: AppliedLimits | None,
    duration_ms: int,
    exit_code: int | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        duration_ms=duration_ms,
        backend=backend,
        limits=limits,
        message=message,
    )


def build_result(
    *,
    outcome: ProcessOutcome,
    nonce: str,
    run_id: str,
    backend: str,
    limits: AppliedLimits,
    config: SandboxConfig,
    duration_ms: int,
) -> ExecutionResult:
    def fail(status: ExecutionStatus, message: str, exit_code: int | None = None) -> ExecutionResult:
        return failure_result(
            run_id=run_id,
            status=status,
            message=message,
            backend=backend,
            limits=limits,
            duration_ms=duration_ms,
            exit_code=exit_code,
        )

    if outcome.stdout_overflow:
        return fail(
            ExecutionStatus.OUTPUT_LIMIT,
            f"sandbox returned more than {config.max_result_bytes} bytes; the run was killed",
        )
    if outcome.timed_out:
        return fail(
            ExecutionStatus.TIMEOUT,
            f"run exceeded the wall-clock limit ({limits.timeout_s:g}s + startup grace) and was killed",
        )
    envelope = extract_envelope(outcome.stdout, nonce)
    if envelope is None:
        if outcome.returncode == OOM_EXIT_CODE:
            return fail(
                ExecutionStatus.OOM,
                "sandbox was killed (exit 137), most likely by the memory limit",
                exit_code=OOM_EXIT_CODE,
            )
        return fail(
            ExecutionStatus.SANDBOX_ERROR,
            f"sandbox returned no valid result (exit code {outcome.returncode})",
            exit_code=outcome.returncode,
        )

    rr = envelope.result
    stdout, stdout_cut = cap_text(rr.stdout, config.max_stdout_bytes)
    stderr, stderr_cut = cap_text(rr.stderr, config.max_stderr_bytes)
    artifacts, dropped = sanitize_artifacts(rr.artifacts, config)
    skipped = [n if is_safe_name(n) else "<invalid name>" for n in rr.skipped_artifacts[:50]]

    notes: list[str] = []
    if rr.timed_out:
        status = ExecutionStatus.TIMEOUT
        notes.append(f"timed out after {limits.timeout_s:g}s; the process was killed")
    elif rr.oom_hint:
        status = ExecutionStatus.OOM
        mem = f" ({limits.memory_mb} MB)" if limits.memory_mb else ""
        notes.append(f"ran out of memory{mem}")
    elif rr.exit_code == 0:
        status = ExecutionStatus.OK
    else:
        status = ExecutionStatus.ERROR
    if skipped or dropped:
        names = sorted(set(skipped + dropped))
        shown = ", ".join(names[:MAX_MESSAGE_ITEMS]) + (" ..." if len(names) > MAX_MESSAGE_ITEMS else "")
        notes.append(f"artifacts not returned (limits, links or invalid): {shown}")

    return ExecutionResult(
        run_id=run_id,
        status=status,
        exit_code=rr.exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=rr.stdout_truncated or stdout_cut,
        stderr_truncated=rr.stderr_truncated or stderr_cut,
        artifacts=artifacts,
        duration_ms=duration_ms,
        backend=backend,
        limits=limits,
        message="; ".join(notes) or None,
    )
