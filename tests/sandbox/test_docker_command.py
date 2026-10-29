"""Snapshot of the hardened `docker run` command. Never weaken it to make a test pass."""

from __future__ import annotations

import pytest

from agentkit.sandbox.config import SandboxConfig
from agentkit.sandbox.docker import build_docker_command

RUN_ID = "0123456789abcdef0123456789abcdef"

EXPECTED = [
    "docker", "run", "--rm", "-i",
    "--name", f"agentkit-sbx-{RUN_ID}",
    "--label", "agentkit.sandbox=1",
    "--label", f"agentkit.run_id={RUN_ID}",
    "--pull", "never",
    "--init",
    "--network", "none",
    "--read-only",
    "--tmpfs", "/workspace:rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m,uid=10001,gid=10001,mode=0700",
    "--user", "10001:10001",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--memory", "512m",
    "--memory-swap", "512m",
    "--cpus", "1",
    "--pids-limit", "64",
    "--ulimit", "nofile=256:256",
    "--ulimit", "nproc=64:64",
    "--hostname", "sandbox",
    "--workdir", "/workspace",
    "--log-driver", "none",
    "--env", "HOME=/tmp",
    "--env", "MPLCONFIGDIR=/tmp/mpl",
    "--env", "PYTHONDONTWRITEBYTECODE=1",
    "--env", "PYTHONUNBUFFERED=1",
    "agentkit-sandbox:dev",
]  # fmt: skip

FORBIDDEN_FLAGS = (
    "--privileged", "-v", "--volume", "--mount", "--env-file", "--cap-add", "--device",
    "--pid", "--ipc", "--uts", "--userns", "--group-add", "-p", "--publish", "-P",
)  # fmt: skip


def test_snapshot() -> None:
    cmd = build_docker_command(SandboxConfig(), RUN_ID)
    image_at = cmd.index("agentkit-sandbox:dev")
    assert cmd[: image_at + 1] == EXPECTED
    assert cmd[image_at + 1 :][0] == "--container"


def test_forbidden_flags_absent() -> None:
    cmd = build_docker_command(SandboxConfig(runtime="runsc"), RUN_ID)
    docker_args = cmd[: cmd.index("agentkit-sandbox:dev")]
    for arg in docker_args:
        flag = arg.split("=", 1)[0]
        assert flag not in FORBIDDEN_FLAGS, arg
    assert docker_args[docker_args.index("--network") + 1] == "none"
    assert "docker.sock" not in " ".join(cmd)
    assert "seccomp" not in " ".join(cmd)  # keep Docker's default profile
    for i, arg in enumerate(docker_args):
        if arg in ("--env", "-e"):
            assert "=" in docker_args[i + 1], "bare -e KEY would copy the host value"
    assert docker_args[-2:] == ["--runtime", "runsc"]


def test_limits_follow_config() -> None:
    cmd = build_docker_command(SandboxConfig(memory_mb=256, cpus=0.5, pids_limit=32), RUN_ID)
    joined = " ".join(cmd)
    assert "--memory 256m --memory-swap 256m" in joined
    assert "--cpus 0.5" in joined and "--pids-limit 32" in joined and "nproc=32:32" in joined


@pytest.mark.parametrize("run_id", ["", "UPPER", "abc; rm -rf /", "--privileged"])
def test_rejects_bad_run_id(run_id: str) -> None:
    with pytest.raises(ValueError):
        build_docker_command(SandboxConfig(), run_id)
