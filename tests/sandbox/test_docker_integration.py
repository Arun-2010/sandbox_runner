"""DockerSandbox against a real daemon and the real image."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from agentkit.sandbox import DockerSandbox, ExecutionRequest, InputFile, SandboxConfig
from agentkit.sandbox.errors import SandboxUnavailableError
from agentkit.sandbox.models import ExecutionStatus
from tests.conftest import IMAGE

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def sandbox() -> DockerSandbox:
    return DockerSandbox(SandboxConfig(image=IMAGE))


async def test_health(sandbox: DockerSandbox) -> None:
    info = await sandbox.health()
    assert info["isolated"] is True and info["image"] == IMAGE


async def test_missing_image_fails_closed() -> None:
    missing = DockerSandbox(SandboxConfig(image="agentkit-sandbox:does-not-exist"))
    with pytest.raises(SandboxUnavailableError, match="docker build"):
        await missing.run(ExecutionRequest(code="print(1)"))


async def test_missing_runtime_fails_closed() -> None:
    box = DockerSandbox(SandboxConfig(image=IMAGE, runtime="no-such-runtime"))
    with pytest.raises(SandboxUnavailableError, match="runtime"):
        await box.run(ExecutionRequest(code="print(1)"))


async def test_hello_world(sandbox: DockerSandbox) -> None:
    result = await sandbox.run(ExecutionRequest(code="print('hello from the sandbox')"))
    assert result.status is ExecutionStatus.OK, result
    assert result.stdout.strip() == "hello from the sandbox" and result.backend == "docker"
    assert result.limits and result.limits.network == "none" and result.limits.memory_mb == 512


async def test_traceback(sandbox: DockerSandbox) -> None:
    result = await sandbox.run(ExecutionRequest(code="1/0"))
    assert result.status is ExecutionStatus.ERROR and "ZeroDivisionError" in result.stderr


async def test_data_libraries_and_chart_artifact(sandbox: DockerSandbox) -> None:
    code = (
        "import pandas as pd, numpy as np, scipy, matplotlib.pyplot as plt\n"
        "df = pd.read_csv('data.csv')\n"
        "print(df['y'].sum())\n"
        "df.plot(x='x', y='y'); plt.savefig('output/chart.png')\n"
    )
    files = [InputFile.from_bytes("data.csv", b"x,y\n1,2\n2,4\n3,6\n")]
    result = await sandbox.run(ExecutionRequest(code=code, files=files, timeout_s=30))
    assert result.status is ExecutionStatus.OK, result.stderr
    assert result.stdout.strip() == "12"
    chart = next(a for a in result.artifacts if a.name == "chart.png")
    assert chart.mime == "image/png" and chart.data().startswith(b"\x89PNG")


async def test_runs_as_sandbox_user(sandbox: DockerSandbox) -> None:
    code = "import os, shutil\nprint(os.getuid(), os.getgid(), shutil.which('pip'), os.getcwd())"
    result = await sandbox.run(ExecutionRequest(code=code))
    assert result.stdout.split() == ["10001", "10001", "None", "/workspace"]


async def test_no_setuid_binaries(sandbox: DockerSandbox) -> None:
    code = (
        "import os, stat\n"
        "hits = []\n"
        "for root in ('/usr', '/bin', '/sbin', '/opt'):\n"
        "    for d, _, fs in os.walk(root):\n"
        "        for f in fs:\n"
        "            try:\n"
        "                m = os.lstat(os.path.join(d, f)).st_mode\n"
        "            except OSError:\n"
        "                continue\n"
        "            if stat.S_ISREG(m) and m & (stat.S_ISUID | stat.S_ISGID):\n"
        "                hits.append(os.path.join(d, f))\n"
        "print(len(hits), hits[:5])"
    )
    result = await sandbox.run(ExecutionRequest(code=code, timeout_s=30))
    assert result.stdout.startswith("0 "), result.stdout


async def test_container_removed_after_run(sandbox: DockerSandbox) -> None:
    result = await sandbox.run(ExecutionRequest(code="print(1)"))
    assert _containers(result.run_id) == []


async def test_concurrent_runs(sandbox: DockerSandbox) -> None:
    requests = [ExecutionRequest(code=f"print({i} * 2)") for i in range(4)]
    results = await asyncio.gather(*(sandbox.run(r) for r in requests))
    assert sorted(int(r.stdout) for r in results) == [0, 2, 4, 6]
    assert len({r.run_id for r in results}) == 4


def _containers(run_id: str) -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=agentkit.run_id={run_id}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout.split()
