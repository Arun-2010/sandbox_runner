# agent-lab

Production patterns for LLM agents, built one project at a time. All code lives in the
`agentkit` package so the projects compose into one system.

| Project | Module | Status |
|---|---|---|
| 1. Strict Pydantic Tool Router | `agentkit/tools.py`, `agentkit/router.py`, `agentkit/llm.py` | done |
| 2. Dynamic Context Budget Assembler | `agentkit/context.py` | done |
| 4. Sandboxed Code Executor | `agentkit/sandbox/`, `agentkit/agents/`, `services/executor/`, `sandbox_image/` | done |

## Layout

```
agentkit/
  tools.py, router.py, llm.py     Project 1: strict tool args, router, LLM protocol, ScriptedLLM
  context.py                      Project 2: token counting, truncate_to_tokens, assemble_context
  sandbox/                        Project 4
    config.py      SandboxConfig (limits; AGENTKIT_SANDBOX_* env vars)
    models.py      requests, results, host<->runner wire format
    errors.py      SandboxError, SandboxUnavailableError, SandboxBusyError, RequestRejectedError
    base.py        Sandbox interface + ManagedSandbox (validation, limiter, payload, logging)
    runner.py      stdlib-only runner executed INSIDE the sandbox
    docker.py      DockerSandbox + hardened `docker run` command builder
    local.py       LocalSubprocessSandbox (UNSAFE, dev/tests only)
    process.py     bounded subprocess I/O with outer timeout and abort hook
    results.py     host-side validation of untrusted runner output
    limiter.py     ConcurrencyLimiter (semaphore + bounded queue)
    janitor.py     removes stale labelled containers
    remote.py      RemoteSandbox (HTTP client for the executor service)
    formatting.py  ExecutionResult -> budgeted text for the LLM
    tool.py        run_python / final_answer tools
  agents/code_interpreter.py      CodeInterpreterAgent
services/executor/                FastAPI executor service, its Dockerfile, docker-compose.yml
sandbox_image/                    sandbox image (Dockerfile, pinned requirements, runner copy)
docs/SECURITY.md                  threat model, layers, limitations, deployment guidance
demo_sandbox.py, demo_attacks.py
tests/                            unit tests; tests/sandbox/test_attacks.py is the attack suite
```

## Commands

```bash
pip install -e ".[dev,sandbox,service]"
pytest                                  # Docker tests auto-skip (with a reason) without Docker/image
pytest -m docker                        # integration + attack suite
docker build -t agentkit-sandbox:dev sandbox_image/
python demo_sandbox.py                  # agent analyses a CSV, saves pnl.png
python demo_attacks.py                  # attack -> result table; non-zero exit if anything gets through
uvicorn services.executor.app:app --port 8080
ruff check . && ruff format --check .
```

`sandbox_image/runner.py` is a copy of `agentkit/sandbox/runner.py` (the image build context is
`sandbox_image/`). After editing the runner: `cp agentkit/sandbox/runner.py sandbox_image/runner.py`.
A test fails if they differ.

---

## Project 4: Sandboxed Code Executor

### Problem

A code-interpreter agent runs Python that an LLM wrote. That code is **untrusted**. It may be
buggy, or it may be hijacked by prompt injection in a file or page the agent read. It must not
be able to read host files or secrets, reach the network, exhaust host resources, persist
between runs, or forge its own result.

### Design

```
agent / router ──► run_python tool ──► Sandbox
                                         ├── DockerSandbox          (production default)
                                         ├── RemoteSandbox          (HTTP → executor service)
                                         └── LocalSubprocessSandbox (UNSAFE, dev/tests only)

DockerSandbox.run(request)
  validate limits → concurrency slot → docker run --rm -i <hardened flags> image
     payload JSON on stdin → runner.py writes /workspace (tmpfs), runs `python -I main.py`
     with inner timeout + rlimits, caps stdout/stderr (head+tail), collects output/* safely,
     kills leftover processes, prints ONE nonce-tagged JSON line
  outer timeout → docker kill; abnormal exit → docker rm -f
  read ≤ max_result_bytes → strict validation → ExecutionResult
```

Every layer and the reasoning behind it is in [docs/SECURITY.md](docs/SECURITY.md). For a
beginner-friendly, end-to-end walkthrough with diagrams and expected output, see
[docs/GUIDE.md](docs/GUIDE.md).

### Usage

```python
from agentkit.sandbox import DockerSandbox, ExecutionRequest, InputFile

sandbox = DockerSandbox()  # SandboxConfig.from_env() for env-driven limits
result = sandbox.run_sync(
    ExecutionRequest(
        code="import pandas as pd; print(pd.read_csv('d.csv').sum())",
        files=[InputFile.from_bytes("d.csv", b"a,b\n1,2\n")],
    )
)
print(result.status, result.stdout, [a.name for a in result.artifacts])
```

Agent:

```python
from agentkit.agents import CodeInterpreterAgent

agent = CodeInterpreterAgent(llm, sandbox, files={"sales.csv": csv_bytes}, max_steps=6)
result = agent.run("Chart monthly profit")  # result.answer, result.steps, result.artifacts
```

Remote (the agent host never touches Docker):

```python
from agentkit.sandbox.remote import RemoteSandbox

sandbox = RemoteSandbox("https://executor.internal:8080", token)
```

### Running it

**Linux**: install Docker Engine, then `docker build -t agentkit-sandbox:dev sandbox_image/` and
`pytest -m docker`. Optional: install gVisor and set `AGENTKIT_SANDBOX_RUNTIME=runsc`.

**macOS**: install Docker Desktop (or Colima/OrbStack), start it, then use the same commands.
Containers run inside a Linux VM.

**Windows**: install Docker Desktop with the WSL 2 backend and start it. Then, in PowerShell:

```powershell
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,sandbox,service]"
docker build -t agentkit-sandbox:dev sandbox_image/
pytest -m docker
```

The POSIX-only runner tests (rlimits, process groups) skip on Windows hosts. Inside the
container the runner always runs on Linux.

**Executor service**

```bash
export AGENTKIT_EXECUTOR_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
uvicorn services.executor.app:app --port 8080
# or: cd services/executor && docker compose build && docker compose up executor

curl -s http://localhost:8080/readyz
curl -s -X POST http://localhost:8080/v1/executions \
  -H "Authorization: Bearer $AGENTKIT_EXECUTOR_TOKEN" -H "Content-Type: application/json" \
  -d '{"code": "print(sum(range(10)))"}'
```

Responses: `200` with an `ExecutionResult` (including `status: rejected` for limit violations),
`401` without a valid token, `413` for oversized bodies, `422` for schema errors, `429` with
`Retry-After` when the queue is full, `503` when Docker or the image is unavailable.

### Configuration

All limits are in `SandboxConfig` and can be set via `AGENTKIT_SANDBOX_<FIELD>` (for example
`AGENTKIT_SANDBOX_MEMORY_MB=1024`). Defaults: 10 s timeout (max 60 s, plus a 5 s startup grace
period), 512 MB RAM with no swap, 1 CPU, 64 PIDs, 64 MB `/workspace`, 32 MB `/tmp`, 64 KB
stdout/stderr, 10 artifacts (5 MB each, 10 MB total), 20 MB result cap, concurrency 4, queue 16.
Every limit has a lower bound, so none can be switched off by accident.

### Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| Isolation by container runtime only | code inspection is bypassable (`getattr`, `exec`, encodings) | AST/regex blocklists |
| Inputs on stdin, `docker run -i` | root FS read-only, `/workspace` is tmpfs; same on Linux/macOS/Windows | bind mounts (host path exposure), `docker cp` (cannot write tmpfs) |
| Docker CLI via `asyncio.create_subprocess_exec` | no shell, argument list, portable stdin | Docker SDK (stdin socket multiplexing), `shell=True` |
| Fresh container per run | no state leaks between runs or users | warm pools that reuse containers |
| Result line tagged with a nonce + strict model | the program shares the runner's UID and can print fake JSON | trusting the last line of stdout |
| OOM from exit 137 / SIGKILL / `MemoryError` | `--rm` removes `State.OOMKilled` | keeping containers for inspection (cleanup race) |
| Bounded queue → 429 | backpressure instead of unbounded latency/memory | unbounded `asyncio.Semaphore` |
| Executor service holds the socket | Docker socket = root | agents with direct Docker access |
| Local backend requires `allow_unsafe=True` | make the unsafe choice explicit and visible | silent fallback when Docker is down |

### Exercises

1. Add gVisor: install `runsc`, set `AGENTKIT_SANDBOX_RUNTIME=runsc`, rerun the attack suite, and compare startup latency.
2. Add a pre-warmed pool that keeps N *unstarted* containers created (never reused) and measure p50 latency.
3. Add a per-tenant quota (runs/minute, CPU-seconds/day) in the executor service.
4. Stream stdout to the client with server-sent events while keeping the same caps.
5. Add the optional AST lint that warns the model about `pip install` / network calls, as UX only.
6. Replace `--ulimit nproc` cross-run interference by enabling userns-remap and verify it with a concurrent fork-bomb test.

### Interview points

- Why "inspect the code" is never a sandbox, and what the real boundary is (namespaces, cgroups, seccomp, capabilities, and the kernel).
- Defence in depth: name the layers and which attack each one stops on its own.
- Fail closed: health checks for daemon, cgroup features, runtime and image, and no unsafe fallback.
- Output is an attack surface: byte caps, strict parsing, the nonce, MIME allow-list, and prompt injection via stdout.
- Timeouts need two levels: inner (process group SIGKILL) and outer (`docker kill`), plus cleanup on cancellation.
- The Docker socket is root: why the executor is a separate service on a dedicated host.
- Shared kernel versus gVisor versus microVMs: the cost and isolation trade-offs for multi-tenant systems.
