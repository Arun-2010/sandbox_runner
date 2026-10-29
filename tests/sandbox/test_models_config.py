from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from agentkit.sandbox import LocalSubprocessSandbox
from agentkit.sandbox.base import check_request
from agentkit.sandbox.config import MB, SandboxConfig
from agentkit.sandbox.errors import RequestRejectedError, SandboxUnavailableError
from agentkit.sandbox.models import ExecutionRequest, ExecutionStatus, InputFile, RunnerResult

B64 = base64.b64encode(b"hello").decode()


@pytest.mark.parametrize(
    "name",
    ["../x", "a/b", "a\\b", "", ".", "..", "x" * 101, "main.py", "output", "sp ace", "naïve.txt", "a\n"],
)
def test_bad_file_names(name: str) -> None:
    with pytest.raises(ValidationError):
        InputFile(name=name, content_b64=B64)


@pytest.mark.parametrize("content", ["not base64!", "aGVsbG8", "aGVs\nbG8="])
def test_bad_base64(content: str) -> None:
    with pytest.raises(ValidationError):
        InputFile(name="a.txt", content_b64=content)


def test_input_file_roundtrip() -> None:
    f = InputFile.from_bytes("data.csv", b"a,b\n1,2\n")
    assert f.data() == b"a,b\n1,2\n" and f.size == 8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": ""},
        {"code": "x" * 100_001},
        {"code": "print(1)", "timeout_s": 0},
        {"code": "print(1)", "unexpected": 1},
        {"code": "print(1)", "files": [{"name": "a", "content_b64": B64}, {"name": "a", "content_b64": B64}]},
    ],
)
def test_bad_requests(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        ExecutionRequest(**kwargs)


@pytest.mark.parametrize(
    ("request_kwargs", "config_kwargs"),
    [
        ({"code": "x" * 200}, {"max_code_chars": 100}),
        ({"code": "1", "files": [InputFile.from_bytes(f"f{i}", b"x") for i in range(3)]}, {"max_input_files": 2}),
        ({"code": "1", "files": [InputFile.from_bytes("big", b"x" * 2000)]}, {"max_input_bytes": 1000}),
        ({"code": "1", "timeout_s": 61}, {}),
    ],
)
def test_check_request_limits(request_kwargs: dict, config_kwargs: dict) -> None:
    with pytest.raises(RequestRejectedError):
        check_request(ExecutionRequest(**request_kwargs), SandboxConfig(**config_kwargs))


def test_rejected_status_from_sandbox() -> None:
    sandbox = LocalSubprocessSandbox(SandboxConfig(max_input_files=0), allow_unsafe=True)
    request = ExecutionRequest(code="print(1)", files=[InputFile.from_bytes("a.txt", b"x")])
    result = sandbox.run_sync(request)
    assert result.status is ExecutionStatus.REJECTED and "limit" in (result.message or "")


def test_config_from_env() -> None:
    env = {"AGENTKIT_SANDBOX_MEMORY_MB": "1024", "AGENTKIT_SANDBOX_RUNTIME": "runsc", "OTHER": "x"}
    cfg = SandboxConfig.from_env(env)
    assert cfg.memory_mb == 1024 and cfg.runtime == "runsc"
    assert SandboxConfig.from_env({"AGENTKIT_SANDBOX_RUNTIME": ""}).runtime is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory_mb": 0},
        {"pids_limit": 0},
        {"timeout_s": 120},
        {"image": "--privileged"},
        {"runtime": "-x"},
        {"max_input_bytes": 100 * MB},
        {"max_result_bytes": 64 * 1024},
        {"unknown": 1},
    ],
)
def test_config_rejects_unsafe_values(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        SandboxConfig(**kwargs)


def test_local_refuses_without_allow_unsafe() -> None:
    with pytest.raises(SandboxUnavailableError, match="allow_unsafe"):
        LocalSubprocessSandbox()


def test_runner_result_is_strict() -> None:
    good = {
        "exit_code": 0, "signal": None, "timed_out": False, "oom_hint": False, "stdout": "",
        "stderr": "", "stdout_truncated": False, "stderr_truncated": False, "stdout_bytes": 0,
        "stderr_bytes": 0, "artifacts": [], "skipped_artifacts": [], "duration_ms": 1,
    }  # fmt: skip
    RunnerResult.model_validate(good)
    with pytest.raises(ValidationError):
        RunnerResult.model_validate({**good, "exit_code": "0"})
    with pytest.raises(ValidationError):
        RunnerResult.model_validate({**good, "injected": True})
