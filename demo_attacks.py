"""Demo: run each attack from the threat model against DockerSandbox and show the result.

Why it exists: a table you can show anyone ("here is hostile code, here is what happened")
is the fastest way to explain, and re-verify, the sandbox's guarantees. Each row has a check
that decides BLOCKED / NOT BLOCKED from the result; the script exits non-zero if any attack
gets through. The pytest attack suite (tests/sandbox/test_attacks.py) asserts the same rows.

    python demo_attacks.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from agentkit.sandbox import DockerSandbox, ExecutionRequest, ExecutionResult, SandboxUnavailableError
from agentkit.sandbox.models import ExecutionStatus as S

CANARY = f"canary-{uuid.uuid4().hex}"


@dataclass
class Attack:
    name: str
    code: str
    blocked: Callable[[ExecutionResult, float], bool]
    timeout_s: float = 10


def no_containers(run_id: str) -> bool:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=agentkit.run_id={run_id}"], capture_output=True, text=True
    )
    return out.stdout.strip() == ""


ATTACKS = [
    Attack(
        "read host files",
        "import os\nprint(open('/etc/hostname').read().strip(), os.path.exists('/var/run/docker.sock'))",
        lambda r, _: r.stdout.split() == ["sandbox", "False"],
    ),
    Attack(
        "read host env vars",
        "import os\nprint('LEAK' if any('canary-' in v for v in os.environ.values()) else 'clean')",
        lambda r, _: r.stdout.strip() == "clean",
    ),
    Attack(
        "network (DNS + TCP)",
        "import socket\n"
        "for f in (lambda: socket.getaddrinfo('example.com', 80),"
        " lambda: socket.create_connection(('1.1.1.1', 80), 3)):\n"
        "    try:\n        f(); print('OPEN')\n    except OSError:\n        print('blocked')",
        lambda r, _: r.stdout.split() == ["blocked", "blocked"],
    ),
    Attack("infinite loop", "while True:\n    pass", lambda r, t: r.status is S.TIMEOUT and t < 12, timeout_s=2),
    Attack("allocate 2 GB", "x = b'a' * (2 * 1024**3)", lambda r, _: r.status is S.OOM, timeout_s=20),
    Attack(
        "fork bomb",
        "import os\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        pass",
        lambda r, t: r.status in (S.TIMEOUT, S.ERROR, S.OOM) and t < 15,
        timeout_s=3,
    ),
    Attack(
        "fill disk (1 GB)",
        "for p in ('/workspace/f', '/tmp/f'):\n"
        "    try:\n        with open(p, 'wb') as fh:\n            for _ in range(1024):\n"
        "                fh.write(b'0' * 2**20)\n        print('WROTE 1GB')\n"
        "    except OSError:\n        print('refused')",
        lambda r, _: r.stdout.split() == ["refused", "refused"],
        timeout_s=20,
    ),
    Attack(
        "print 100 MB",
        "import sys\nfor _ in range(100 * 1024):\n    sys.stdout.write('x' * 1023 + '\\n')",
        lambda r, _: r.stdout_truncated and len(r.stdout.encode()) <= 64 * 1024,
        timeout_s=30,
    ),
    Attack(
        "write /usr, /etc, /opt/runner",
        "for p in ('/usr/x', '/etc/x', '/opt/runner/runner.py'):\n"
        "    try:\n        open(p, 'w').write('x'); print('WROTE')\n    except OSError:\n        print('denied')",
        lambda r, _: r.stdout.split() == ["denied"] * 3,
    ),
    Attack(
        "privilege escalation",
        "import os\ntry:\n    os.setuid(0); print('ROOT')\nexcept PermissionError:\n    print('denied')\n"
        "print([l.split()[1] for l in open('/proc/self/status') if l.startswith('CapEff')][0])",
        lambda r, _: r.stdout.split() == ["denied", "0000000000000000"],
    ),
    Attack(
        "artifact symlink -> /etc/passwd",
        "import os\nos.symlink('/etc/passwd', 'output/passwd.txt')",
        lambda r, _: not r.artifacts,
    ),
    Attack(
        "ignore SIGTERM, sleep forever",
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(10**6)",
        lambda r, _: r.status is S.TIMEOUT,
        timeout_s=2,
    ),
    Attack(
        "leave background process",
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], start_new_session=True)",
        lambda r, t: r.status is S.OK and t < 15 and no_containers(r.run_id),
    ),
    Attack(
        "forge runner output",
        "import os\ntry:\n    open(f'/proc/{os.getppid()}/fd/1', 'w').write('{\"nonce\":\"x\"}\\n')\n"
        "    print('WROTE')\nexcept OSError:\n    print('denied')\nraise SystemExit(5)",
        lambda r, _: r.status is S.ERROR and r.exit_code == 5 and "denied" in r.stdout,
    ),
]


def main() -> int:
    os.environ["AGENTKIT_DEMO_CANARY"] = CANARY  # set on the host; must not appear inside
    sandbox = DockerSandbox()
    try:
        sandbox.health_sync()
    except SandboxUnavailableError as exc:
        print(f"sandbox unavailable: {exc}", file=sys.stderr)
        return 1

    # Persistence needs two runs, so it is checked separately.
    sandbox.run_sync(ExecutionRequest(code="open('/workspace/p','w').write('x'); open('/tmp/p','w').write('x')"))
    second = sandbox.run_sync(
        ExecutionRequest(code="import os\nprint(os.path.exists('/workspace/p') or os.path.exists('/tmp/p'))")
    )
    rows = [("persistence across runs", second.status.value, second.stdout.strip() == "False", 0.0)]

    for attack in ATTACKS:
        start = time.monotonic()
        result = sandbox.run_sync(ExecutionRequest(code=attack.code, timeout_s=attack.timeout_s))
        elapsed = time.monotonic() - start
        rows.append((attack.name, result.status.value, attack.blocked(result, elapsed), elapsed))

    width = max(len(r[0]) for r in rows)
    print(f"{'attack':<{width}}  {'status':<13} {'time':>6}  result")
    print("-" * (width + 36))
    for name, status, ok, elapsed in rows:
        print(f"{name:<{width}}  {status:<13} {elapsed:5.1f}s  {'BLOCKED' if ok else 'NOT BLOCKED'}")
    failed = [r[0] for r in rows if not r[2]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} attacks blocked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
