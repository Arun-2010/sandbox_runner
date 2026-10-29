"""Runner behaviour through LocalSubprocessSandbox (no Docker needed)."""

from __future__ import annotations

import os
import sys
import time

import pytest

from agentkit.sandbox import ExecutionRequest, InputFile, LocalSubprocessSandbox, SandboxConfig
from agentkit.sandbox.config import KB
from agentkit.sandbox.models import ExecutionStatus

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX-only runner feature")


@pytest.fixture
def sandbox() -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(
        SandboxConfig(
            max_stdout_bytes=4 * KB,
            max_artifacts=3,
            max_artifact_bytes=1 * KB,
            max_total_artifact_bytes=2 * KB,
        ),
        allow_unsafe=True,
    )


async def run(sandbox: LocalSubprocessSandbox, code: str, **kwargs):
    return await sandbox.run(ExecutionRequest(code=code, **kwargs))


async def test_hello_world(sandbox) -> None:
    result = await run(sandbox, "print('hello world')")
    assert result.status is ExecutionStatus.OK and result.exit_code == 0
    assert result.stdout.strip() == "hello world" and result.backend == "local"
    assert result.limits and result.limits.isolated is False


async def test_exception_traceback(sandbox) -> None:
    result = await run(sandbox, "def f():\n    raise ValueError('boom')\nf()")
    assert result.status is ExecutionStatus.ERROR and result.exit_code == 1
    assert "Traceback" in result.stderr and "ValueError: boom" in result.stderr


async def test_input_files(sandbox) -> None:
    files = [InputFile.from_bytes("data.csv", b"a,b\n1,2\n")]
    result = await run(sandbox, "print(open('data.csv').read().splitlines()[1])", files=files)
    assert result.stdout.strip() == "1,2"


async def test_inner_timeout(sandbox) -> None:
    start = time.monotonic()
    result = await run(sandbox, "while True:\n    pass", timeout_s=1)
    assert result.status is ExecutionStatus.TIMEOUT
    assert time.monotonic() - start < 1 + sandbox.config.startup_grace_s


async def test_stdout_cap_keeps_head_and_tail(sandbox) -> None:
    code = "print('HEAD')\nfor i in range(20000):\n    print('x' * 50)\nprint('TAIL')"
    result = await run(sandbox, code)
    assert result.status is ExecutionStatus.OK and result.stdout_truncated
    assert result.stdout.startswith("HEAD") and result.stdout.rstrip().endswith("TAIL")
    assert len(result.stdout.encode()) <= sandbox.config.max_stdout_bytes + 64


async def test_artifacts_collected(sandbox) -> None:
    code = "open('output/result.csv', 'w').write('a,b\\n')\nopen('output/notes.txt', 'w').write('hi')"
    result = await run(sandbox, code)
    by_name = {a.name: a for a in result.artifacts}
    assert set(by_name) == {"result.csv", "notes.txt"}
    assert by_name["result.csv"].mime == "text/csv" and by_name["notes.txt"].data() == b"hi"


async def test_too_many_and_too_large_artifacts(sandbox) -> None:
    code = (
        "for i in range(5):\n    open(f'output/f{i}.txt', 'w').write('x')\n"
        "open('output/big.bin', 'wb').write(b'x' * 5000)\n"
        "import os; os.mkdir('output/subdir')"
    )
    result = await run(sandbox, code)
    names = {a.name for a in result.artifacts}
    assert len(names) <= sandbox.config.max_artifacts and "big.bin" not in names
    assert "not returned" in (result.message or "")


async def test_total_artifact_cap(sandbox) -> None:
    code = "for i in range(3):\n    open(f'output/f{i}.bin', 'wb').write(b'x' * 900)"
    result = await run(sandbox, code)
    assert sum(a.size for a in result.artifacts) <= sandbox.config.max_total_artifact_bytes
    assert len(result.artifacts) == 2


async def test_symlink_in_output_skipped(sandbox, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    code = f"import os\nos.symlink({str(secret)!r}, 'output/leak.txt')\nprint('linked')"
    result = await run(sandbox, code)
    if "linked" not in result.stdout:
        pytest.skip("cannot create symlinks on this platform (Windows without developer mode)")
    assert all(a.name != "leak.txt" for a in result.artifacts)
    assert all(b"TOP SECRET" not in a.data() for a in result.artifacts)


async def test_hardlink_in_output_skipped(sandbox, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    code = f"import os\nos.link({str(secret)!r}, 'output/leak.txt')\nprint('linked')"
    result = await run(sandbox, code)
    if "linked" not in result.stdout:
        pytest.skip("hard link across filesystems not possible here")
    assert all(a.name != "leak.txt" for a in result.artifacts)


async def test_child_env_is_minimal(sandbox, monkeypatch) -> None:
    monkeypatch.setenv("AGENTKIT_TEST_CANARY", "canary-local-123")
    result = await run(sandbox, "import os\nprint(sorted(os.environ))\nprint(os.environ.get('AGENTKIT_TEST_CANARY'))")
    assert "canary-local-123" not in result.stdout


async def test_stderr_and_exit_code(sandbox) -> None:
    result = await run(sandbox, "import sys\nsys.stderr.write('bad\\n')\nsys.exit(3)")
    assert result.status is ExecutionStatus.ERROR and result.exit_code == 3 and "bad" in result.stderr


async def test_memory_error_reports_oom(sandbox) -> None:
    result = await run(sandbox, "raise MemoryError()")
    assert result.status is ExecutionStatus.OOM


async def test_forged_stdout_line_does_not_become_result(sandbox) -> None:
    fake = '{"nonce":"0000000000000000","result":{"exit_code":0}}'
    result = await run(sandbox, f"print({fake!r})\nraise SystemExit(4)")
    assert result.status is ExecutionStatus.ERROR and result.exit_code == 4


@POSIX_ONLY
async def test_background_process_killed_with_group(sandbox, tmp_path) -> None:
    marker = tmp_path / "alive"
    code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2); open({str(marker)!r}, \"w\")'])\n"
        "print('spawned')"
    )
    result = await run(sandbox, code)
    assert result.status is ExecutionStatus.OK
    time.sleep(3)
    assert not marker.exists()


@POSIX_ONLY
async def test_rlimit_fsize(sandbox) -> None:
    cfg = SandboxConfig(workspace_mb=8, max_input_bytes=1024)
    small = LocalSubprocessSandbox(cfg, allow_unsafe=True)
    code = "try:\n    open('big', 'wb').write(b'x' * (20 * 1024 * 1024))\nexcept OSError as e:\n    print('EFBIG')"
    result = await run(small, code)
    assert "EFBIG" in result.stdout


def test_runner_rejects_bad_payload() -> None:
    import subprocess

    from agentkit.sandbox.local import RUNNER_PATH

    for payload in (b"not json", b'{"version": 1}', b"x" * 2000):
        proc = subprocess.run(
            [sys.executable, "-I", str(RUNNER_PATH), "--workspace", "unused", "--max-payload-bytes", "1000"],
            input=payload,
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 2 and b"runner error" in proc.stderr and proc.stdout == b""
