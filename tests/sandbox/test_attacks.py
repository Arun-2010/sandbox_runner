"""Attack suite: each test plays the role of hostile LLM-written code and asserts it is contained.

If one of these fails, fix the isolation. Never loosen the assertion.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from agentkit.sandbox import DockerSandbox, ExecutionRequest, SandboxConfig
from agentkit.sandbox.models import ExecutionStatus
from tests.conftest import IMAGE

pytestmark = pytest.mark.docker
SLACK_S = 5.0


@pytest.fixture(scope="module")
def sandbox() -> DockerSandbox:
    return DockerSandbox(SandboxConfig(image=IMAGE))


def run(sandbox: DockerSandbox, code: str, timeout_s: float | None = None):
    return sandbox.run_sync(ExecutionRequest(code=code, timeout_s=timeout_s))


def report(result) -> dict:
    assert result.status is ExecutionStatus.OK, (result.status, result.message, result.stderr[-2000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_host_files_not_visible(sandbox) -> None:
    home = str(Path.home())
    code = f"""
import json, os
print(json.dumps({{
    "hostname": open('/etc/hostname').read().strip(),
    "docker_sock": os.path.exists('/var/run/docker.sock') or os.path.exists('/run/docker.sock'),
    "host_home": os.path.exists({home!r}),
    "home_entries": os.listdir('/home') if os.path.isdir('/home') else [],
    "root_home_readable": os.access('/root', os.R_OK),
    "mounts": [l.split()[1] for l in open('/proc/mounts')],
}}))
"""
    data = report(run(sandbox, code))
    assert data["hostname"] == "sandbox" and data["hostname"] != socket.gethostname(), data
    assert data["docker_sock"] is False and data["host_home"] is False, data
    assert data["home_entries"] == [] and data["root_home_readable"] is False, data
    bad = [m for m in data["mounts"] if "docker.sock" in m or m.startswith(("/host", "/Users", "/mnt/c"))]
    assert bad == [], data


def test_host_env_vars_not_visible(sandbox, monkeypatch) -> None:
    canary = f"canary-{uuid.uuid4().hex}"
    monkeypatch.setenv("AGENTKIT_CANARY", canary)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", canary)
    result = run(sandbox, "import os, json\nprint(json.dumps(dict(os.environ)))")
    env = report(result)
    assert canary not in result.stdout
    assert "AGENTKIT_CANARY" not in env and "AWS_SECRET_ACCESS_KEY" not in env


def test_network_blocked(sandbox) -> None:
    code = """
import json, socket, urllib.request
out = {}
def attempt(name, fn):
    try:
        fn()
        out[name] = "OPEN"
    except Exception as exc:
        out[name] = "blocked: " + type(exc).__name__
attempt("dns", lambda: socket.getaddrinfo("example.com", 80))
attempt("http", lambda: urllib.request.urlopen("http://1.1.1.1", timeout=3))
attempt("tcp", lambda: socket.create_connection(("8.8.8.8", 53), timeout=3))
attempt("udp", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("8.8.8.8", 53)))
ifaces = [l.split(":")[0].strip() for l in open("/proc/net/dev").readlines()[2:]]
out["interfaces"] = ifaces
print(json.dumps(out))
"""
    data = report(run(sandbox, code, timeout_s=20))
    for key in ("dns", "http", "tcp", "udp"):
        assert data[key].startswith("blocked"), (key, data[key])
    assert data["interfaces"] == ["lo"]


def test_infinite_loop_times_out(sandbox) -> None:
    start = time.monotonic()
    result = run(sandbox, "while True:\n    pass", timeout_s=2)
    elapsed = time.monotonic() - start
    assert result.status is ExecutionStatus.TIMEOUT
    assert elapsed < 2 + sandbox.config.startup_grace_s + SLACK_S


def test_memory_bomb_is_oom(sandbox) -> None:
    result = run(sandbox, "x = b'a' * (2 * 1024 ** 3)\nprint(len(x))", timeout_s=20)
    assert result.status is ExecutionStatus.OOM, (result.status, result.message, result.stderr[-500:])


def test_fork_bomb_contained(sandbox) -> None:
    code = """
import os
while True:
    try:
        os.fork()
    except OSError:
        pass
"""
    start = time.monotonic()
    result = run(sandbox, code, timeout_s=3)
    assert time.monotonic() - start < 3 + sandbox.config.startup_grace_s + SLACK_S
    assert result.status in (ExecutionStatus.TIMEOUT, ExecutionStatus.ERROR, ExecutionStatus.OOM)
    assert run(sandbox, "print('host fine')").stdout.strip() == "host fine"  # next run unaffected


def test_disk_fill_limited(sandbox) -> None:
    code = """
import json
out = {}
for path in ("/workspace/fill.bin", "/tmp/fill.bin"):
    written = 0
    try:
        with open(path, "wb") as fh:
            chunk = b"\\0" * (1024 * 1024)
            for _ in range(1024):
                fh.write(chunk)
                written += 1
    except OSError as exc:
        out[path] = {"mb": written, "error": type(exc).__name__}
    else:
        out[path] = {"mb": written, "error": None}
print(json.dumps(out))
"""
    data = report(run(sandbox, code, timeout_s=30))
    cfg = sandbox.config
    assert data["/workspace/fill.bin"]["error"] and data["/workspace/fill.bin"]["mb"] <= cfg.workspace_mb
    assert data["/tmp/fill.bin"]["error"] and data["/tmp/fill.bin"]["mb"] <= cfg.tmp_mb


def test_huge_stdout_truncated(sandbox) -> None:
    code = "import sys\nline = 'x' * 999 + '\\n'\nfor _ in range(100 * 1024):\n    sys.stdout.write(line)"
    result = run(sandbox, code, timeout_s=30)
    assert result.status is ExecutionStatus.OK and result.stdout_truncated
    assert len(result.stdout.encode()) <= sandbox.config.max_stdout_bytes


def test_cannot_write_system_paths(sandbox) -> None:
    code = """
import json
out = {}
for path in ("/usr/evil", "/etc/evil", "/opt/runner/evil", "/opt/runner/runner.py", "/evil"):
    try:
        with open(path, "w") as fh:
            fh.write("x")
        out[path] = "WRITTEN"
    except OSError as exc:
        out[path] = type(exc).__name__
print(json.dumps(out))
"""
    data = report(run(sandbox, code))
    assert all(v != "WRITTEN" for v in data.values()), data


def test_privilege_escalation_fails(sandbox) -> None:
    code = """
import json, os
out = {}
for name, fn in (("setuid", lambda: os.setuid(0)), ("setgid", lambda: os.setgid(0))):
    try:
        fn()
        out[name] = "ESCALATED"
    except PermissionError:
        out[name] = "denied"
status = dict(l.split(":", 1) for l in open("/proc/self/status") if ":" in l)
for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs"):
    out[key] = status.get(key, "").strip()
out["uid"] = os.getuid()
print(json.dumps(out))
"""
    data = report(run(sandbox, code))
    assert data["setuid"] == "denied" and data["setgid"] == "denied" and data["uid"] == 10001
    for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(data[key], 16) == 0, (key, data[key])
    assert data["NoNewPrivs"] == "1"


def test_no_persistence_between_runs(sandbox) -> None:
    first = run(sandbox, "open('/workspace/persist.txt','w').write('x')\nopen('/tmp/persist.txt','w').write('x')")
    assert first.status is ExecutionStatus.OK
    code = "import os\nprint(os.path.exists('/workspace/persist.txt'), os.path.exists('/tmp/persist.txt'))"
    assert run(sandbox, code).stdout.split() == ["False", "False"]


def test_artifact_symlink_not_returned(sandbox) -> None:
    code = (
        "import os\n"
        "os.symlink('/etc/passwd', 'output/passwd.txt')\n"
        "os.symlink('/opt/runner', 'output/runner_dir')\n"
        "try:\n    os.link('/etc/passwd', 'output/hard.txt')\nexcept OSError:\n    pass\n"
        "open('output/ok.txt', 'w').write('fine')\n"
    )
    result = run(sandbox, code)
    names = {a.name for a in result.artifacts}
    assert names == {"ok.txt"}
    assert all(b"root:" not in a.data() for a in result.artifacts)


def test_sigterm_ignoring_process_killed(sandbox) -> None:
    code = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(10_000)"
    start = time.monotonic()
    result = run(sandbox, code, timeout_s=2)
    assert result.status is ExecutionStatus.TIMEOUT
    assert time.monotonic() - start < 2 + sandbox.config.startup_grace_s + SLACK_S


def test_background_process_does_not_survive(sandbox) -> None:
    code = """
import os, subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        import time
        time.sleep(600)
    os._exit(0)
print("spawned")
"""
    start = time.monotonic()
    result = run(sandbox, code)
    assert result.status is ExecutionStatus.OK and "spawned" in result.stdout
    assert time.monotonic() - start < 15
    remaining = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=agentkit.run_id={result.run_id}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert remaining.stdout.split() == []


def test_forged_runner_output(sandbox) -> None:
    fake = {
        "nonce": "f" * 32,
        "result": {
            "exit_code": 0, "signal": None, "timed_out": False, "oom_hint": False,
            "stdout": "FORGED", "stderr": "", "stdout_truncated": False, "stderr_truncated": False,
            "stdout_bytes": 6, "stderr_bytes": 0, "skipped_artifacts": [], "duration_ms": 1,
            "artifacts": [{"name": "pwned.txt", "size": 1, "content_b64": "eA=="}],
        },
    }  # fmt: skip
    code = f"""
import json, os, sys
fake = {json.dumps(fake)!r}
attempts = {{}}
for target in (f"/proc/{{os.getppid()}}/fd/1", "/proc/1/fd/1"):
    try:
        with open(target, "w") as fh:
            fh.write(fake + "\\n" + "G" * 100_000 + "\\n")
        attempts[target] = "WROTE"
    except OSError as exc:
        attempts[target] = type(exc).__name__
print(fake)
sys.stderr.write(json.dumps(attempts))
sys.exit(7)
"""
    result = run(sandbox, code)
    assert result.status is ExecutionStatus.ERROR and result.exit_code == 7, (result.status, result.message)
    assert all(a.name != "pwned.txt" for a in result.artifacts)
    assert len(result.stdout.encode()) <= sandbox.config.max_stdout_bytes
    attempts = json.loads(result.stderr)
    # The runner is non-dumpable, so its stdout cannot be opened via /proc. PID 1 (docker-init)
    # runs as the same UID and is writable; that is why the host requires the per-run nonce.
    runner_fd = next(k for k in attempts if k != "/proc/1/fd/1")
    assert attempts[runner_fd] == "PermissionError", attempts
    assert "FORGED" not in result.stdout.replace(json.dumps(fake), "")


def test_flooding_container_stdout_is_capped(sandbox) -> None:
    code = """
chunk = "G" * (1024 * 1024) + "\\n"
with open("/proc/1/fd/1", "w") as fh:
    for _ in range(40):
        fh.write(chunk)
"""
    result = run(sandbox, code, timeout_s=30)
    assert result.status in (ExecutionStatus.OUTPUT_LIMIT, ExecutionStatus.SANDBOX_ERROR), result.status
    assert result.stdout == "" and result.artifacts == []


@pytest.mark.skipif(os.environ.get("AGENTKIT_RUN_SLOW") != "1", reason="slow; set AGENTKIT_RUN_SLOW=1")
def test_many_sequential_runs_leave_nothing_behind(sandbox) -> None:
    for _ in range(20):
        assert run(sandbox, "print(1)").status is ExecutionStatus.OK
    leftover = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=agentkit.sandbox=1"], capture_output=True, text=True, timeout=30
    )
    assert leftover.stdout.split() == []
