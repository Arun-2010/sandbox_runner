"""Host-side validation of untrusted runner output."""

from __future__ import annotations

import base64
import json

import pytest

from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.models import AppliedLimits, ExecutionStatus
from agentkit.sandbox.process import ProcessOutcome
from agentkit.sandbox.results import build_result, cap_text, guess_mime

NONCE = "a" * 32
CFG = SandboxConfig(max_stdout_bytes=1024, max_artifacts=2, max_artifact_bytes=100, max_total_artifact_bytes=150)
LIMITS = AppliedLimits(isolated=True, network="none", timeout_s=10, max_stdout_bytes=1024, max_stderr_bytes=1024)


def runner_result(**overrides) -> dict:
    result = {
        "exit_code": 0, "signal": None, "timed_out": False, "oom_hint": False, "stdout": "hi",
        "stderr": "", "stdout_truncated": False, "stderr_truncated": False, "stdout_bytes": 2,
        "stderr_bytes": 0, "artifacts": [], "skipped_artifacts": [], "duration_ms": 3,
    }  # fmt: skip
    result.update(overrides)
    return result


def line(result: dict, nonce: str = NONCE) -> bytes:
    return json.dumps({"nonce": nonce, "result": result}).encode()


def build(stdout: bytes, returncode: int | None = 0, **outcome) -> object:
    po = ProcessOutcome(
        returncode=returncode,
        stdout=stdout,
        stderr=b"",
        timed_out=outcome.get("timed_out", False),
        stdout_overflow=outcome.get("stdout_overflow", False),
        stdout_total=len(stdout),
    )
    return build_result(outcome=po, nonce=NONCE, run_id="r1", backend="test", limits=LIMITS, config=CFG, duration_ms=1)


def test_valid_result() -> None:
    result = build(line(runner_result()))
    assert result.status is ExecutionStatus.OK and result.stdout == "hi"


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"garbage\x00\xff",
        b'{"nonce": "' + NONCE.encode() + b'"}',
        line(runner_result(exit_code="0")),
        line(runner_result(extra_field=1)),
        line(runner_result(), nonce="b" * 32),
        line(runner_result())[:-5],
    ],
)
def test_garbage_or_forged_output_is_sandbox_error(stdout: bytes) -> None:
    result = build(stdout, returncode=0)
    assert result.status is ExecutionStatus.SANDBOX_ERROR
    assert result.stdout == "" and result.stderr == ""  # never echo raw output


def test_last_valid_nonce_line_wins_over_forgery() -> None:
    forged = line(runner_result(stdout="forged"), nonce="c" * 32)
    real = line(runner_result(stdout="real"))
    assert build(forged + b"\n" + real + b"\n").stdout == "real"


def test_oversized_and_timeout_and_oom() -> None:
    assert build(b"x", stdout_overflow=True).status is ExecutionStatus.OUTPUT_LIMIT
    assert build(b"", returncode=None, timed_out=True).status is ExecutionStatus.TIMEOUT
    assert build(b"", returncode=137).status is ExecutionStatus.OOM
    assert build(line(runner_result(oom_hint=True, exit_code=137))).status is ExecutionStatus.OOM
    assert build(line(runner_result(timed_out=True))).status is ExecutionStatus.TIMEOUT


def test_host_recaps_streams() -> None:
    result = build(line(runner_result(stdout="y" * 10_000)))
    assert result.stdout_truncated and len(result.stdout.encode()) <= CFG.max_stdout_bytes


def art(name: str, data: bytes, size: int | None = None) -> dict:
    return {"name": name, "size": len(data) if size is None else size, "content_b64": base64.b64encode(data).decode()}


def test_artifact_sanitisation() -> None:
    artifacts = [
        art("../etc/passwd", b"x"),
        art("ok.png", b"p" * 10),
        art("lie.txt", b"abc", size=1),
        art("big.bin", b"b" * 101),
        art("ok.png", b"dupe"),
        art("page.html", b"<script>"),
        art("third.txt", b"t"),
    ]
    result = build(line(runner_result(artifacts=artifacts)))
    assert [a.name for a in result.artifacts] == ["ok.png", "page.html"]
    assert result.artifacts[1].mime == "application/octet-stream"
    assert "not returned" in (result.message or "")


def test_cap_text_and_mime() -> None:
    text, cut = cap_text("é" * 1000, 101)
    assert cut and len(text.encode()) <= 101 + 40
    assert guess_mime("a.PNG") == "image/png" and guess_mime("x.svg") == "application/octet-stream"
